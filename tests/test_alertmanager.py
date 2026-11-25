import copy

from fastapi.testclient import TestClient

from app.main import create_app
from tests import fakes
from tests.conftest import TOKEN

EXC_LINE = {"time": "2026-11-24T05:12:29.512+00:00", "level": "ERROR", "logger": "app.error",
            "message": "처리하지 못한 오류 ZeroDivisionError", "request_id": fakes.REQUEST_ID, "exc_info": fakes.TRACE}
WEB_TAIL = ["▲ Next.js 15.5.4", "Error: Missing required env NEXT_PUBLIC_API_BASE_URL"]
EVENTS = [{"reason": "BackOff", "message": "Back-off restarting failed container web", "lastTimestamp": "2026-11-24T05:12:50Z"},
          {"reason": "Pulled", "message": "Container image already present", "lastTimestamp": "2026-11-24T05:12:10Z"}]


def jobs(c) -> dict:
    return {job.incident.alertname: job for _, job in c.queue.consume("t", count=10, block_ms=10)}


def test_maemusae_webhook_becomes_incidents_with_evidence_and_last_deploy(container, webhook):
    loki, queries = fakes.loki(exc_lines=[EXC_LINE], pod_lines=WEB_TAIL)
    container.__dict__.update(loki=loki, kube=fakes.kube(EVENTS))
    client = TestClient(create_app(container), headers=TOKEN)

    res = client.post("/api/v1/alerts/alertmanager", json=webhook)
    assert res.status_code == 202
    body = res.json()
    assert len(body["accepted"]) == 2 and body["resolved"] == [], "해소 알림은 열린 장애가 없으면 아무것도 바꾸지 않는다"

    by_name = jobs(container)
    exc = by_name["MaemusaeUnhandledException"].incident
    assert (exc.app, exc.namespace, exc.severity) == ("api", "maemusae", "critical")
    assert exc.evidence.stacktrace == fakes.TRACE and exc.evidence.request_id == fakes.REQUEST_ID
    assert exc.deploy.image == fakes.image("api", "a2") and exc.deploy.previous == fakes.image("api", "a1")
    assert exc.deploy.deployed_at.isoformat() == "2026-11-24T05:08:00+00:00"

    crash = by_name["KubePodCrashLooping"].incident
    assert crash.app == "web", "kube-state-metrics의 service 라벨이 아니라 container"
    assert crash.evidence.stacktrace is None and crash.evidence.pod_logs.endswith("NEXT_PUBLIC_API_BASE_URL\n")
    assert crash.evidence.pod_events == ["Pulled: Container image already present", "BackOff: Back-off restarting failed container web"]
    assert crash.deploy.previous is None, "web 이미지는 이력 안에서 바뀐 적이 없다"
    assert queries == ['{namespace="maemusae", container="api"} | json | logger="app.error"',
                       '{namespace="maemusae", pod="web-7c9f8d6b5d-x2kqp"}']


def test_repeated_alert_and_resolution(container, webhook):
    client = TestClient(create_app(container), headers=TOKEN)
    first = client.post("/api/v1/alerts/alertmanager", json=webhook).json()
    for key in container.redis.scan_iter("dedupe:*"):
        container.redis.delete(key)
    again = client.post("/api/v1/alerts/alertmanager", json=webhook).json()
    assert again["accepted"] == [] and sorted(again["already_open"]) == sorted(first["accepted"]), \
        "Alertmanager가 repeat_interval 뒤 다시 보내도 열린 장애에 붙는다"

    resolved = copy.deepcopy(webhook)
    resolved["alerts"] = [{**webhook["alerts"][0], "status": "resolved", "endsAt": "2026-11-24T05:30:00Z"}]
    body = client.post("/api/v1/alerts/alertmanager", json=resolved).json()
    assert len(body["resolved"]) == 1
    assert client.post("/api/v1/alerts/alertmanager", json=resolved).json()["resolved"] == [], "해소는 한 번만"
    kinds = [job.kind for _, job in container.queue.consume("t", count=10, block_ms=10)]
    assert kinds.count("resolved") == 1


def test_same_fault_on_two_replicas_is_one_incident(container, webhook):
    crash = webhook["alerts"][1]
    other = {**crash, "fingerprint": "other-pod", "labels": {**crash["labels"], "pod": "web-7c9f8d6b5d-9wzrt"}}
    body = TestClient(create_app(container), headers=TOKEN).post(
        "/api/v1/alerts/alertmanager", json={**webhook, "alerts": [crash, other]}).json()
    assert len(body["accepted"]) == 1 and body["deduplicated"] == body["accepted"]


def test_lookup_failures_still_accept(container, webhook):
    loki, _ = fakes.loki(fail=True)
    container.__dict__.update(loki=loki, infra=None)
    body = TestClient(create_app(container), headers=TOKEN).post("/api/v1/alerts/alertmanager", json=webhook).json()
    exc = jobs(container)["MaemusaeUnhandledException"].incident
    assert len(body["accepted"]) == 2 and exc.evidence.stacktrace is None and exc.deploy is None


def test_tokens_are_separate(container, webhook):
    client = TestClient(create_app(container))
    url = "/api/v1/alerts/alertmanager"
    assert client.post(url, json=webhook, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post(url, json=webhook, headers={"Authorization": "Bearer sec-token"}).status_code == 401, \
        "보안 보고 토큰으로 장애를 만들 수 없다"
    assert client.post(url, json=webhook, headers=TOKEN).status_code == 202


def test_malformed_payload_is_rejected_at_the_boundary(container):
    res = TestClient(create_app(container), headers=TOKEN).post("/api/v1/alerts/alertmanager", json={"alerts": [{"status": "firing"}]})
    assert res.status_code == 422
