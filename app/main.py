"""Webhook API(3단계 접수). Alertmanager 알림과 보안 스캔 결과를 받는다.

알림은 근거(스택트레이스, Pod 로그와 이벤트, 직전 배포)를 붙여 큐에 넣고 바로 202를 돌려준다.
보안 결과는 비밀값을 버리고 security DB에 쌓는다. 요약은 워커가 하루에 한 번 한다.
실행: uvicorn app.main:app
"""
import hmac
import logging
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST

from app.alerts import Alert, AlertmanagerPayload, Need
from app.container import Container
from app.core.fingerprint import fingerprint
from app.deploys import ImagesFileError, deploy_from_history
from app.incident import AnalyzeJob, Deploy, Evidence, Incident, ResolvedJob
from app.security.findings import SecurityReportIn, Source, parse

log = logging.getLogger("api")


def collect_evidence(c: Container, alert: Alert, now: datetime) -> Evidence:
    """조회가 실패해도 접수는 한다. 각 클라이언트가 실패를 None이나 빈 목록으로 돌려준다."""
    needs, ev = alert.kind.needs, Evidence()
    pod = alert.labels.get("pod")
    if Need.STACKTRACE in needs and c.loki:
        if exc := c.loki.latest_exception(alert.namespace, alert.app, alert.starts_at, now):
            ev.stacktrace, ev.request_id = exc.stacktrace, exc.request_id
    if Need.POD_LOGS in needs and c.loki and pod:
        ev.pod_logs = c.loki.pod_tail(alert.namespace, pod, alert.starts_at, now)
    if Need.POD_EVENTS in needs and c.kube and pod:
        ev.pod_events = c.kube.pod_events(alert.namespace, pod)
    return ev


def last_deploy(c: Container, app: str) -> Deploy | None:
    if c.infra is None:
        return None
    try:
        return deploy_from_history(app, c.infra.history(c.settings.deploy_history_depth))
    except (httpx.HTTPError, ImagesFileError) as e:
        log.warning("deploy history of %s failed: %s", app, e)
        return None


def accept(c: Container, inc: Incident) -> tuple[str, bool]:
    """(장애 ID, 중복 여부). 같은 에러가 알림 폭주로 여러 번 와도 분석은 한 번만 돈다."""
    fp = fingerprint(inc)
    incident_id = uuid.uuid4().hex[:12]
    if existing := c.store.claim_fingerprint(fp, incident_id, c.settings.dedupe_ttl_sec):
        c.metrics.received.labels(deduplicated="true").inc()
        return existing, True
    c.store.create(incident_id, fp, inc)
    c.queue.enqueue(AnalyzeJob(incident_id=incident_id, fingerprint=fp, incident=inc))
    c.metrics.received.labels(deduplicated="false").inc()
    return incident_id, False


def create_app(container: Container | None = None) -> FastAPI:
    app = FastAPI(title="OPS-maemusae", version="0.1.0")
    app.state.container = container or Container()

    def get_container(request: Request) -> Container:
        return request.app.state.container

    def bearer(expected: str, authorization: str) -> None:
        token = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    def alertmanager_auth(authorization: str = Header(default=""), c: Container = Depends(get_container)) -> None:
        bearer(c.settings.alertmanager_token, authorization)

    def security_auth(authorization: str = Header(default=""), c: Container = Depends(get_container)) -> None:
        bearer(c.settings.security_report_token, authorization)

    @app.get("/healthz")
    def healthz(c: Container = Depends(get_container)) -> dict:
        c.redis.ping()
        for pool in (c.db, c.security_db):
            with pool.connection() as conn:
                conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def metrics(c: Container = Depends(get_container)) -> Response:
        return Response(c.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/v1/alerts/alertmanager", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(alertmanager_auth)])
    def alertmanager_webhook(payload: AlertmanagerPayload, c: Container = Depends(get_container)) -> dict:
        accepted, deduplicated, already_open, resolved = [], [], [], []
        now = datetime.now(timezone.utc)
        for alert in payload.alerts:
            if alert.status == "resolved":
                for incident_id in c.store.resolve_alert(alert.fingerprint, alert.ends_at or now):
                    c.queue.enqueue(ResolvedJob(incident_id=incident_id, resolved_at=alert.ends_at or now))
                    resolved.append(incident_id)
                continue
            if open_id := c.store.open_alert_incident(alert.fingerprint):
                already_open.append(open_id)
                continue
            inc = Incident(alertname=alert.name, alert_fingerprint=alert.fingerprint, app=alert.app, namespace=alert.namespace,
                           severity=alert.labels.get("severity", "unknown"), message=alert.summary,
                           occurred_at=alert.starts_at, evidence=collect_evidence(c, alert, now),
                           deploy=last_deploy(c, alert.app))
            incident_id, dup = accept(c, inc)
            (deduplicated if dup else accepted).append(incident_id)
        return {"accepted": accepted, "deduplicated": deduplicated, "already_open": already_open, "resolved": resolved}

    @app.post("/api/v1/security/{source}", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(security_auth)])
    def security_report(source: Source, body: SecurityReportIn, c: Container = Depends(get_container)) -> dict:
        try:
            findings = parse(source, body)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{source} report: {e}") from e
        run_id = c.security.record_run(source, body.target, body.run_url, findings)
        return {"run_id": run_id, "findings": len(findings)}

    @app.get("/api/v1/incidents/{incident_id}", dependencies=[Depends(alertmanager_auth)])
    def get_incident(incident_id: str, c: Container = Depends(get_container)) -> dict:
        record = c.store.get(incident_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
        return {"incident_id": record.id, "status": record.status, "fingerprint": record.fingerprint,
                "incident": record.incident.model_dump(mode="json"), "actions": c.store.actions(incident_id), **record.result}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
