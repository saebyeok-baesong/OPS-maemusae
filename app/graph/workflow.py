"""LangGraph 워크플로.

START -> classify(경량) -> retrieve_context(런북 RAG)
  -> [코드 수정 불필요] report (런북 안내)
  -> [코드 수정 필요] analyze_root_cause(고성능, 스택트레이스 위치의 SERVICE 코드를 같이 본다)
       -> [SERVICE 소스 없음] report
       -> generate_fix(고성능) -> analyze_diff(diff + 영향 범위)
            -> [diff 형식이 아니고 시도 남음] generate_fix (거절 사유를 붙여 다시)
            -> report
예산을 넘으면 어느 LLM 노드에서든 report로 빠진다. 롤백 제안은 그래프 밖(app/rollback.py)에서 배포 이력만으로 정한다.
수정안은 SERVICE에 적용하지도 테스트하지도 않는다. 사람이 보고 PR로 올린다.
"""
from langgraph.graph import END, START, StateGraph

from app.graph.nodes import Nodes
from app.graph.state import IncidentState

RECURSION_LIMIT = 15


def _stopped(s: IncidentState) -> bool:
    return s.get("outcome") == "budget_exceeded"


def _needs_fix(s: IncidentState) -> bool:
    cls = s["classification"]
    return cls["needs_code_fix"] and cls["confidence"] >= 0.5


def build_graph(nodes: Nodes, max_fix_attempts: int = 2, checkpointer=None):
    g = StateGraph(IncidentState)
    g.add_node("classify", nodes.classify)
    g.add_node("retrieve_context", nodes.retrieve_context)
    g.add_node("analyze_root_cause", nodes.analyze_root_cause)
    g.add_node("generate_fix", nodes.generate_fix)
    g.add_node("analyze_diff", nodes.analyze_diff)
    g.add_node("report", nodes.report)

    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", lambda s: "report" if _stopped(s) else "retrieve_context",
                            ["report", "retrieve_context"])
    g.add_conditional_edges("retrieve_context", lambda s: "analyze_root_cause" if _needs_fix(s) else "report",
                            ["analyze_root_cause", "report"])
    g.add_conditional_edges("analyze_root_cause",
                            lambda s: "generate_fix" if s.get("source") and not _stopped(s) else "report",
                            ["generate_fix", "report"])
    g.add_conditional_edges("generate_fix", lambda s: "report" if _stopped(s) else "analyze_diff",
                            ["analyze_diff", "report"])

    def after_diff(s: IncidentState) -> str:
        if s.get("impact") or s.get("fix_attempts", 0) >= max_fix_attempts:
            return "report"
        return "generate_fix"

    g.add_conditional_edges("analyze_diff", after_diff, ["generate_fix", "report"])
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)
