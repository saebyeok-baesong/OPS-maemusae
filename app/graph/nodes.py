"""LangGraph 노드. 각 노드는 상태의 일부만 갱신해서 돌려준다."""
import functools
import logging

from app.analysis import code_context
from app.analysis.diff import analyze_diff
from app.analysis.impact import ImportGraph, analyze_impact, is_test_path
from app.config import Settings
from app.core.fingerprint import normalize
from app.graph.state import IncidentState
from app.incident import Incident
from app.llm import prompts
from app.llm.cost import BudgetExceeded, CostTracker
from app.llm.router import LLMRouter
from app.llm.schemas import Classification, FixProposal, RootCause
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
    def __init__(self, settings: Settings, router: LLMRouter, retriever: Retriever, cost: CostTracker, source=None):
        self.s = settings
        self.router = router
        self.retriever = retriever
        self.cost = cost
        self.source = source  # app.sources.service.ServiceSource | None

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

    def _checkout(self, inc: Incident) -> dict | None:
        """이 앱의 SERVICE 소스를 받는다. 못 받으면 코드 없이 원인 분석만 한다(장애 보고를 막지 않는다)."""
        subdir = self.s.service_source_dirs.get(inc.app)
        if not (self.source and subdir):
            return None
        try:
            sha = self.source.resolve()
            self.source.checkout(sha, subdir)
            return {"sha": sha, "dir": subdir}
        except Exception:  # noqa: BLE001
            log.exception("SERVICE source checkout failed")
            return None

    def _root(self, source: dict) -> str:
        return str(self.source.checkout(source["sha"], source["dir"]))

    @budget_guard
    def analyze_root_cause(self, state: IncidentState) -> dict:
        inc = Incident.model_validate(state["incident"])
        source = state.get("source") or self._checkout(inc)
        code = code_context.from_stacktrace(self._root(source), inc.evidence.stacktrace) if source else ""
        docs = "\n\n".join(f"[{d['doc_type']}:{d['doc_id']}]\n{d['text']}" for d in state.get("context_docs", []))
        cache_key = f"{state['fingerprint']}:{inc.deploy and inc.deploy.image}:{source and source['sha']}"
        result = self.router.invoke("root_cause", RootCause, prompts.ROOT_CAUSE_SYSTEM,
                                    prompts.root_cause_user(inc, state["classification"], docs, code),
                                    subject=state["incident_id"], cache_key=cache_key)
        return {"root_cause": result.model_dump(), **({"source": source} if source else {})}

    @budget_guard
    def generate_fix(self, state: IncidentState) -> dict:
        inc, rc, root = Incident.model_validate(state["incident"]), state["root_cause"], self._root(state["source"])
        code = (code_context.from_files(root, rc.get("suspected_files") or [])
                or code_context.from_stacktrace(root, inc.evidence.stacktrace))
        history = state.get("fix_history", [])
        result = self.router.invoke("generate_fix", FixProposal, prompts.FIX_SYSTEM,
                                    prompts.fix_user(inc, rc, code, history), subject=state["incident_id"])
        return {"fix": result.model_dump(), "fix_attempts": state.get("fix_attempts", 0) + 1}

    def analyze_diff(self, state: IncidentState) -> dict:
        """diff를 읽어 바뀐 심볼을 찾고, import 그래프로 영향 범위를 잰다. diff가 아니면 사유를 남겨 다시 만들게 한다."""
        root, patch = self._root(state["source"]), state["fix"]["patch"]
        diff = analyze_diff(patch, root)
        if not diff.files:
            reason = "unified diff 형식이 아니라서 바뀐 파일을 하나도 찾지 못했다. '--- a/경로' '+++ b/경로' 헤더와 @@ hunk가 있어야 한다."
            return {"diff": diff.to_dict(), "fix_history": [*state.get("fix_history", []), {"patch": patch, "reason": reason}]}
        return {"diff": diff.to_dict(), "impact": analyze_impact(diff, ImportGraph(root)).to_dict()}

    def report(self, state: IncidentState) -> dict:
        outcome = state.get("outcome") or (
            "fix_proposed" if state.get("impact") else "analyzed" if state.get("root_cause") else "runbook")
        cost = self.cost.summary(state["incident_id"])
        return {"outcome": outcome, "cost": cost, "report": render_report({**state, "outcome": outcome, "cost": cost})}


OUTCOME_TITLE = {
    "runbook": "코드 수정 대상 아님 · 런북 안내",
    "analyzed": "코드 수정 필요 · 원인 분석",
    "fix_proposed": "코드 수정 필요 · 원인 분석과 수정안",
    "budget_exceeded": "LLM 비용 상한 도달 · 담당자 확인",
}


def impact_summary(impact: dict, limit: int = 5) -> list[str]:
    """영향 범위 요약 줄. 깊이 1(직접 import)부터."""
    src = sorted((d, m) for m, d in impact["impacted"].items() if not is_test_path(m.replace(".", "/") + ".py"))
    tests = len(impact["impacted"]) - len(src)
    lines = [f"위험도 {impact['risk']} · 바뀐 모듈 {', '.join(impact['changed_modules']) or '-'} · 영향 받는 모듈 {len(src)}개, 테스트 {tests}개"]
    lines += [f"- {m} (깊이 {d})" for d, m in src[:limit]]
    if len(src) > limit:
        lines.append(f"- 외 {len(src) - limit}개")
    return lines


def fix_lines(state: dict) -> list[str]:
    fix, src = state.get("fix"), state.get("source")
    if not fix:
        return []
    if not state.get("impact"):
        return [f"수정안: {state.get('fix_attempts', 0)}번 만들었지만 diff 형식이 아니어서 버렸다"]
    files = ", ".join(f"{src['dir']}/{f['path']} (+{f['added']} -{f['removed']})" for f in state["diff"]["files"])
    return [f"수정안 (SERVICE {src['sha'][:7]} 기준, 위험도 {fix['risk']}): {fix['explanation']}", f"바뀐 파일: {files}",
            "영향 범위: " + "\n".join(impact_summary(state["impact"], limit=20)), fix["patch"]]


def render_report(state: dict) -> str:
    """조회 API에 남기는 전문. Discord 메시지는 이것의 요약이다."""
    inc = Incident.model_validate(state["incident"])
    cls = state.get("classification") or {}
    lines = [f"[{OUTCOME_TITLE.get(state['outcome'], state['outcome'])}] {inc.app}", f"> {inc.message.splitlines()[0][:200]}"]
    if cls:
        lines.append(f"분류: {cls['category']} (신뢰도 {cls['confidence']:.2f}) - {cls['summary']}")
    if rc := state.get("root_cause"):
        lines.append(f"원인: {rc['root_cause']}")
        lines += [f"- 근거: {e}" for e in rc.get("evidence", [])]
        lines += [f"- 할 일: {s}" for s in rc.get("next_steps", [])]
    lines += fix_lines(state)
    if docs := state.get("context_docs"):
        lines.append("런북: " + ", ".join(dict.fromkeys(d["doc_id"] for d in docs)))
    if state.get("error"):
        lines.append(f"중단 사유: {state['error']}")
    cost = state.get("cost") or {}
    lines.append(f"LLM 비용: ${cost.get('usd', 0):.4f} (캐시 적중 {cost.get('cache_hits', 0)}회)")
    return "\n".join(lines)
