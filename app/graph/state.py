from typing import TypedDict


class IncidentState(TypedDict, total=False):
    """체크포인터가 저장하는 상태라서 JSON 직렬화가 되는 값만 둔다. incident는 Incident.model_dump(mode="json")."""

    incident_id: str
    incident: dict
    fingerprint: str

    classification: dict
    context_docs: list[dict]
    root_cause: dict

    # 코드 수정안. source는 {"sha", "dir"}: 한 장애의 단계들이 같은 SERVICE 커밋을 본다
    source: dict
    fix: dict             # FixProposal
    fix_attempts: int
    fix_history: list[dict]   # 거절된 수정안과 사유
    diff: dict            # DiffAnalysis.to_dict()
    impact: dict          # ImpactReport.to_dict()

    outcome: str      # runbook | analyzed | fix_proposed | budget_exceeded
    error: str
    report: str
    cost: dict
