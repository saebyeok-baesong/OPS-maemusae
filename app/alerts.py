"""Alertmanager webhook을 장애로 바꾼다. 알림 종류 표 하나가 대상, 근거 수집, 롤백 여부를 정한다."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Need(StrEnum):
    STACKTRACE = "stacktrace"   # Loki의 app.error 로그에서 exc_info, request_id
    POD_LOGS = "pod_logs"       # 그 Pod의 마지막 로그
    POD_EVENTS = "pod_events"   # 그 Pod의 Kubernetes 이벤트(OOMKilled, BackOff)


@dataclass(frozen=True)
class AlertKind:
    target_labels: tuple[str, ...]
    needs: frozenset[Need] = frozenset()
    rollback: bool = False      # 배포 직후에 났으면 직전 이미지로 롤백을 제안한다


POD = ("container", "pod")
# 알림 이름과 라벨은 docs/alerting.md의 규칙과 맞춘다. 바꿀 곳은 이 표 하나다
ALERT_KINDS: dict[str, AlertKind] = {
    "MaemusaeUnhandledException": AlertKind(("app", "container"), frozenset({Need.STACKTRACE}), rollback=True),
    "MaemusaeHighErrorRate": AlertKind(("app", "container"), frozenset({Need.STACKTRACE}), rollback=True),
    "KubePodCrashLooping": AlertKind(POD, frozenset({Need.POD_LOGS, Need.POD_EVENTS}), rollback=True),
    "KubePodOOMKilled": AlertKind(POD, frozenset({Need.POD_LOGS, Need.POD_EVENTS})),
}
DEFAULT_KIND = AlertKind(("app", "container", "service", "pod", "namespace"))


def _parse_time(value: str | None) -> datetime | None:
    # 해소되지 않은 알림의 endsAt은 0001-01-01이다
    if not value or value.startswith("0001"):
        return None
    return datetime.fromisoformat(value)


class Alert(BaseModel):
    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(None, alias="endsAt")
    fingerprint: str

    @field_validator("ends_at", mode="before")
    @classmethod
    def _unset_end(cls, v):
        return _parse_time(v) if isinstance(v, str) else v

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unknown")

    @property
    def kind(self) -> AlertKind:
        return ALERT_KINDS.get(self.name, DEFAULT_KIND)

    @property
    def app(self) -> str:
        return next((self.labels[k] for k in self.kind.target_labels if self.labels.get(k)), "unknown")

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace", "")

    @property
    def summary(self) -> str:
        text = self.annotations.get("summary") or self.annotations.get("description") or self.name
        return f"[{self.name}] {text}"


class AlertmanagerPayload(BaseModel):
    """Alertmanager webhook v4. 쓰는 필드만 받는다."""

    status: str
    receiver: str = ""
    alerts: list[Alert]
