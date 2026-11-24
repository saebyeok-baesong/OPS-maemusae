"""장애 한 건과 큐 메시지의 모양. API가 만들고 워커와 승인 봇이 읽는다."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


class Deploy(BaseModel):
    """알림 시점에 INFRA `images.yaml`이 가리키던 이미지와 그 직전 이미지(contracts/images-yaml.md)."""

    app: str
    image: str
    previous: str | None
    deployed_at: datetime
    commit: str
    by_rollback: bool = False


class Evidence(BaseModel):
    stacktrace: str | None = None
    request_id: str | None = None
    pod_logs: str | None = None
    pod_events: list[str] = Field(default_factory=list)


class Incident(BaseModel):
    alertname: str
    alert_fingerprint: str
    app: str
    namespace: str
    severity: str
    message: str
    occurred_at: datetime
    evidence: Evidence = Field(default_factory=Evidence)
    deploy: Deploy | None = None


class AnalyzeJob(BaseModel):
    kind: Literal["analyze"] = "analyze"
    incident_id: str
    fingerprint: str
    incident: Incident


class ResolvedJob(BaseModel):
    """알림이 해소됐다. 카드에 해소 확인을 남긴다."""

    kind: Literal["resolved"] = "resolved"
    incident_id: str
    resolved_at: datetime


QueueJob = Annotated[AnalyzeJob | ResolvedJob, Field(discriminator="kind")]
