from typing import TypedDict


class IncidentState(TypedDict, total=False):
    """체크포인터가 저장하는 상태라서 JSON 직렬화가 되는 값만 둔다. incident는 Incident.model_dump(mode="json")."""

    incident_id: str
    incident: dict
    fingerprint: str

    classification: dict
    context_docs: list[dict]
    root_cause: dict

    outcome: str      # runbook | analyzed | budget_exceeded
    error: str
    report: str
    cost: dict
