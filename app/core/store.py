"""장애 기록(incident DB)과 중복 제거 키(Redis)."""
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from redis import Redis

from app.incident import Incident

TERMINAL = {"runbook", "analyzed", "budget_exceeded", "failed"}


@dataclass(frozen=True)
class IncidentRecord:
    id: str
    status: str
    fingerprint: str
    incident: Incident
    result: dict
    card_channel_id: str | None
    card_message_id: str | None
    alert_resolved_at: datetime | None


class IncidentStore:
    def __init__(self, pool: ConnectionPool, redis: Redis):
        self.pool = pool
        self.redis = redis

    def claim_fingerprint(self, fp: str, incident_id: str, ttl_sec: int) -> str | None:
        """처음 보는 지문이면 None, 이미 처리 중인 지문이면 기존 장애 ID."""
        key = f"dedupe:{fp}"
        if self.redis.set(key, incident_id, nx=True, ex=ttl_sec):
            return None
        existing = self.redis.get(key)
        return existing.decode() if isinstance(existing, bytes) else existing

    def create(self, incident_id: str, fp: str, inc: Incident) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO incidents (id, fingerprint, alertname, alert_fingerprint, app, namespace, severity, message,
                                          occurred_at, incident, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')""",
                (incident_id, fp, inc.alertname, inc.alert_fingerprint, inc.app, inc.namespace, inc.severity, inc.message,
                 inc.occurred_at, Jsonb(inc.model_dump(mode="json"))),
            )

    def open_alert_incident(self, alert_fingerprint: str) -> str | None:
        """같은 알림의 장애가 아직 해소되지 않았으면 그 ID. Alertmanager는 해소 전까지 같은 알림을 다시 보낸다."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT id FROM incidents WHERE alert_fingerprint = %s AND alert_resolved_at IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (alert_fingerprint,),
            ).fetchone()
        return row["id"] if row else None

    def resolve_alert(self, alert_fingerprint: str, resolved_at: datetime) -> list[str]:
        """처음 해소로 바뀐 장애 ID. 해소 알림이 다시 와도 빈 목록이다."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                """UPDATE incidents SET alert_resolved_at = %s
                   WHERE alert_fingerprint = %s AND alert_resolved_at IS NULL RETURNING id""",
                (resolved_at, alert_fingerprint),
            ).fetchall()
        return [r["id"] for r in rows]

    def set_status(self, incident_id: str, status: str, *, report: str | None = None, **result) -> None:
        cls = result.get("classification") or {}
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE incidents SET status = %s, updated_at = now(), report = coalesce(%s, report),
                     category = coalesce(%s, category), needs_code_fix = coalesce(%s, needs_code_fix),
                     result = result || %s,
                     finished_at = CASE WHEN %s THEN now() ELSE finished_at END
                   WHERE id = %s""",
                (status, report, cls.get("category"), cls.get("needs_code_fix"), Jsonb(result), status in TERMINAL,
                 incident_id),
            )

    def get(self, incident_id: str) -> IncidentRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT id, status, fingerprint, incident, result, card_channel_id, card_message_id, alert_resolved_at
                   FROM incidents WHERE id = %s""",
                (incident_id,),
            ).fetchone()
        if not row:
            return None
        return IncidentRecord(row["id"], row["status"], row["fingerprint"], Incident.model_validate(row["incident"]),
                              row["result"], row["card_channel_id"], row["card_message_id"], row["alert_resolved_at"])

    def set_card(self, incident_id: str, channel_id: str, message_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE incidents SET card_channel_id = %s, card_message_id = %s WHERE id = %s",
                         (channel_id, message_id, incident_id))

    def start_action(self, incident_id: str, action: str, target: str, approver: str) -> bool:
        """처음 승인이면 True. 같은 장애의 같은 조치는 한 번만 실행한다(두 사람이 동시에 눌러도)."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO actions (incident_id, action, target, approver, status)
                   VALUES (%s, %s, %s, %s, 'running')
                   ON CONFLICT (incident_id, action) DO NOTHING RETURNING id""",
                (incident_id, action, target, approver),
            ).fetchone()
        return row is not None

    def finish_action(self, incident_id: str, action: str, status: str, detail: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE actions SET status = %s, detail = %s, finished_at = now() WHERE incident_id = %s AND action = %s",
                (status, detail, incident_id, action),
            )

    def actions(self, incident_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT action, target, approver, status, detail, approved_at, finished_at FROM actions WHERE incident_id = %s",
                (incident_id,),
            ).fetchall()
