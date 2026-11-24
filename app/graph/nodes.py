"""LangGraph 노드. 각 노드는 상태의 일부만 갱신해서 돌려준다."""
import functools
import logging

from app.config import Settings
from app.core.fingerprint import normalize
from app.graph.state import IncidentState
from app.incident import Incident
from app.llm import prompts
from app.llm.cost import BudgetExceeded, CostTracker
from app.llm.router import LLMRouter
from app.llm.schemas import Classification, RootCause
from app.rag.retriever import Retriever, SearchFilter

log = logging.getLogger(__name__)


def budget_guard(fn):
    """예산을 넘으면 그래프를 멈추고 보고 단계로 보낸다."""

    @functools.wraps(fn)
    def wrapper(self, state: IncidentState) -> dict:
        try:
            return fn(self, state)
        except BudgetExceeded as e:
            log.warning("budget exceeded: %s", e)
            return {"outcome": "budget_exceeded", "error": str(e)}

    return wrapper


class Nodes:
    def __init__(self, settings: Settings, router: LLMRouter, retriever: Retriever, cost: CostTracker):
        self.s = settings
        self.router = router
        self.retriever = retriever
        self.cost = cost

    @budget_guard
    def classify(self, state: IncidentState) -> dict:
        inc = Incident.model_validate(state["incident"])
        result = self.router.invoke("classify", Classification, prompts.CLASSIFY_SYSTEM, prompts.classify_user(inc),
                                    subject=state["incident_id"], cache_key=state["fingerprint"])
        return {"classification": result.model_dump()}

    def retrieve_context(self, state: IncidentState) -> dict:
        inc, cls = Incident.model_validate(state["incident"]), state["classification"]
        # 가변 값을 지운 질의라야 같은 에러가 재발했을 때 검색 캐시가 맞는다
        query = " ".join([cls["summary"], *cls.get("keywords", []), normalize(inc.message.splitlines()[0][:200])])
        flt = SearchFilter(service=inc.app, categories=[cls["category"], "general"], since_days=self.s.retrieval_lookback_days)
        hits = self.retriever.search(query, flt, top_k=self.s.retrieval_top_k)
        return {"context_docs": [{"doc_id": h.doc_id, "score": h.score, "doc_type": h.doc_type,
                                  "text": h.text[: self.s.context_chunk_max_chars]} for h in hits]}

    @budget_guard
    def analyze_root_cause(self, state: IncidentState) -> dict:
        inc = Incident.model_validate(state["incident"])
        docs = "\n\n".join(f"[{d['doc_type']}:{d['doc_id']}]\n{d['text']}" for d in state.get("context_docs", []))
        result = self.router.invoke("root_cause", RootCause, prompts.ROOT_CAUSE_SYSTEM,
                                    prompts.root_cause_user(inc, state["classification"], docs),
                                    subject=state["incident_id"], cache_key=f"{state['fingerprint']}:{inc.deploy and inc.deploy.image}")
        return {"root_cause": result.model_dump()}

    def report(self, state: IncidentState) -> dict:
        outcome = state.get("outcome") or ("analyzed" if state.get("root_cause") else "runbook")
        cost = self.cost.summary(state["incident_id"])
        return {"outcome": outcome, "cost": cost, "report": render_report({**state, "outcome": outcome, "cost": cost})}


OUTCOME_TITLE = {
    "runbook": "코드 수정 대상 아님 · 런북 안내",
    "analyzed": "코드 수정 필요 · 원인 분석",
    "budget_exceeded": "LLM 비용 상한 도달 · 담당자 확인",
}


def render_report(state: dict) -> str:
    """조회 API에 남기는 전문. Discord 카드는 이것의 요약이다."""
    inc = Incident.model_validate(state["incident"])
    cls = state.get("classification") or {}
    lines = [f"[{OUTCOME_TITLE.get(state['outcome'], state['outcome'])}] {inc.app}", f"> {inc.message.splitlines()[0][:200]}"]
    if cls:
        lines.append(f"분류: {cls['category']} (신뢰도 {cls['confidence']:.2f}) - {cls['summary']}")
    if rc := state.get("root_cause"):
        lines.append(f"원인: {rc['root_cause']}")
        lines += [f"- 근거: {e}" for e in rc.get("evidence", [])]
        lines += [f"- 할 일: {s}" for s in rc.get("next_steps", [])]
    if docs := state.get("context_docs"):
        lines.append("런북: " + ", ".join(dict.fromkeys(d["doc_id"] for d in docs)))
    if state.get("error"):
        lines.append(f"중단 사유: {state['error']}")
    cost = state.get("cost") or {}
    lines.append(f"LLM 비용: ${cost.get('usd', 0):.4f} (캐시 적중 {cost.get('cache_hits', 0)}회)")
    return "\n".join(lines)
