"""알림 접수부터 Discord 메시지까지. 워커를 직접 부른다."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rag.loader import load_markdown_dir
from app.worker import holding, process
from tests import fakes
from tests.conftest import TOKEN
from tests.test_alertmanager import EXC_LINE, WEB_TAIL


def post(c, webhook, *alerts):
    body = {**webhook, "alerts": [webhook["alerts"][i] for i in alerts]}
    return TestClient(create_app(c), headers=TOKEN).post("/api/v1/alerts/alertmanager", json=body).json()


def drain(c):
    return [process(c, mid, job) for mid, job in c.queue.consume("w", count=10, block_ms=10)]


@pytest.fixture
def evidence(container):
    loki, _ = fakes.loki(exc_lines=[EXC_LINE], pod_lines=WEB_TAIL)
    container.__dict__.update(loki=loki)
    container.ingestor.ingest(load_markdown_dir("knowledge"))
    return container


def test_code_bug_right_after_deploy_gets_root_cause_and_a_rollback_button(evidence, webhook, llm_calls, github):
    c = evidence
    [incident_id] = post(c, webhook, 0)["accepted"]
    [final] = drain(c)

    assert final["outcome"] == "analyzed"
    assert llm_calls == [("fake:light", "Classification"), ("fake:heavy", "RootCause")]
    assert len(github.pulls) == 1 and final["rollback_pr"].plan.app == "api"
    [(channel, card, reply_to)] = c.discord.posts
    assert channel == "C-INC" and reply_to is None
    [button, link] = card["components"][0]["components"]
    assert button["custom_id"] == f"rollback:{incident_id}" and link["url"] == final["rollback_pr"].url
    names = [f["name"] for f in card["embeds"][0]["fields"]]
    assert names[:3] == ["앱", "분류", "LLM 비용"] and {"원인", "근거", "롤백 제안"} <= set(names)
    record = c.store.get(incident_id)
    assert record.card_message_id == "m1" and record.result["rollback_pr"]["number"] == 1


def test_crashloop_without_code_fix_uses_only_the_light_model_and_links_runbooks(evidence, webhook, llm_calls):
    post(evidence, webhook, 1)
    [final] = drain(evidence)
    assert final["outcome"] == "runbook" and llm_calls == [("fake:light", "Classification")]
    assert final["rollback_pr"] is None, "web은 이력 안에서 배포가 없었다"
    card = evidence.discord.posts[0][1]
    runbooks = next(f["value"] for f in card["embeds"][0]["fields"] if f["name"] == "런북")
    assert "knowledge/k8s/crashloop.md" in runbooks and "components" not in card


def test_runbook_branch_also_gets_a_rollback_button_right_after_a_deploy(evidence, webhook, llm_calls, github):
    crash_at = datetime.fromisoformat(webhook["alerts"][1]["startsAt"])
    github.commit("main", fakes.images_yaml(web=fakes.image("web", "w2"), api=fakes.image("api", "a2")),
                  "deploy(web): w2 (#8)", crash_at - timedelta(minutes=3))
    [incident_id] = post(evidence, webhook, 1)["accepted"]
    [final] = drain(evidence)

    assert final["outcome"] == "runbook" and llm_calls == [("fake:light", "Classification")]
    assert final["rollback_pr"].plan.app == "web"
    card = evidence.discord.posts[0][1]
    assert card["components"][0]["components"][0]["custom_id"] == f"rollback:{incident_id}"


def test_same_error_again_hits_the_classification_cache(evidence, webhook, llm_calls):
    first = post(evidence, webhook, 1)["accepted"][0]
    drain(evidence)
    evidence.store.resolve_alert(webhook["alerts"][1]["fingerprint"], datetime.now(timezone.utc))
    evidence.redis.delete(*evidence.redis.keys("dedupe:*"))
    llm_calls.clear()
    second = post(evidence, webhook, 1)["accepted"][0]
    drain(evidence)
    assert second != first and llm_calls == []
    assert evidence.store.get(second).result["cost"]["cache_hits"] == 1


def test_budget_cap_stops_before_the_heavy_model(evidence, webhook, llm_calls):
    evidence.settings.incident_budget_usd = 0.000001
    evidence.__dict__.pop("cost", None)
    evidence.__dict__.pop("router", None)
    evidence.__dict__.pop("graph", None)
    post(evidence, webhook, 0)
    [final] = drain(evidence)
    assert final["outcome"] == "budget_exceeded" and llm_calls == [("fake:light", "Classification")]
    assert evidence.discord.posts[0][1]["embeds"][0]["title"].startswith("💸")


def test_resolved_alert_replies_on_the_card(evidence, webhook):
    [incident_id] = post(evidence, webhook, 0)["accepted"]
    drain(evidence)
    resolved = {**webhook["alerts"][0], "status": "resolved", "endsAt": "2026-11-24T05:40:30Z"}
    TestClient(create_app(evidence), headers=TOKEN).post("/api/v1/alerts/alertmanager", json={**webhook, "alerts": [resolved]})
    [text] = drain(evidence)
    assert text == "✅ 알림 해소 확인 · 발생 28분 뒤"
    assert evidence.discord.posts[-1][2] == "m1", "메시지에 답으로 단다"
    assert evidence.store.get(incident_id).alert_resolved_at is not None


def test_redelivered_message_does_not_post_a_second_card(evidence, webhook, github):
    post(evidence, webhook, 0)
    [(msg_id, job)] = list(evidence.queue.consume("w1", count=1, block_ms=10))
    process(evidence, msg_id, job)
    process(evidence, msg_id, job)
    assert len(evidence.discord.posts) == 1 and len(github.pulls) == 1


class WorkerKilled(BaseException):
    """kill -9처럼 except Exception에 잡히지 않는 중단."""


def test_killed_worker_resumes_from_the_checkpoint(evidence, webhook, llm_calls, monkeypatch):
    from app.graph.nodes import Nodes

    original = Nodes.analyze_root_cause
    calls = {"n": 0}

    def flaky(self, state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WorkerKilled
        return original(self, state)

    monkeypatch.setattr(Nodes, "analyze_root_cause", flaky)
    evidence.__dict__.pop("graph", None)
    [incident_id] = post(evidence, webhook, 0)["accepted"]
    [(msg_id, job)] = list(evidence.queue.consume("w1", count=1, block_ms=10))
    with pytest.raises(WorkerKilled):
        process(evidence, msg_id, job)
    [(msg_id2, job2)] = evidence.queue.reclaim_stale("w2", min_idle_ms=0)
    final = process(evidence, msg_id2, job2)

    assert final["outcome"] == "analyzed"
    assert [m for m, _ in llm_calls] == ["fake:light", "fake:heavy"], "분류는 다시 부르지 않는다"
    assert evidence.store.get(incident_id).result["resumed_from"] == ["analyze_root_cause"]


def test_message_being_processed_is_not_reclaimed(evidence, webhook):
    import time

    post(evidence, webhook, 1)
    [(msg_id, _)] = evidence.queue.consume("w1", count=1, block_ms=10)
    with holding(evidence.queue, "w1", msg_id, claim_idle_ms=300):
        time.sleep(0.6)
        assert evidence.queue.reclaim_stale("w2", min_idle_ms=300) == []
    time.sleep(0.4)
    assert [m for m, _ in evidence.queue.reclaim_stale("w2", min_idle_ms=300)] == [msg_id], "붙잡던 워커가 죽으면 회수된다"
