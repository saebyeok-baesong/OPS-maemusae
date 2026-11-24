from datetime import timedelta

import httpx

from app.approvals import handle_click
from app.discord import cards
from app.discord.client import DiscordClient
from app.incident import Evidence
from app.rollback import RollbackPR
from tests import fakes
from tests.conftest import INFRA_ROLE
from tests.test_rollback import PLAN, incident, with_proposal


def state(outcome="analyzed", **extra) -> dict:
    inc = incident().model_copy(update={"evidence": Evidence(stacktrace=fakes.TRACE, request_id=fakes.REQUEST_ID,
                                                              pod_events=["BackOff: x"])})
    return {"incident": inc.model_dump(mode="json"), "outcome": outcome,
            "classification": {"category": "code_bug", "confidence": 0.85, "summary": "0으로 나눔"},
            "root_cause": {"root_cause": "빈 주문 목록에서 평균을 낸다", "next_steps": ["빈 목록 처리"]},
            "context_docs": [{"doc_id": "api/unhandled-exception.md"}, {"doc_id": "api/unhandled-exception.md"}],
            "cost": {"usd": 0.0123}, **extra}


PR = RollbackPR(plan=PLAN, number=7, url="https://github.com/saebyeok-baesong/INFRA-maemusae/pull/7", head_sha="abc")


def test_incident_card_has_summary_evidence_runbook_links_and_a_button():
    msg = cards.incident_card("inc1", state(), PR, "https://github.com/o/OPS/blob/main/knowledge")
    [embed] = msg["embeds"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert embed["title"] == "🔎 코드 수정 필요 · 원인 분석"
    assert "ZeroDivisionError: division by zero" in fields["근거"] and fakes.REQUEST_ID in fields["근거"]
    assert "배포:" in fields["근거"] and "(4분 전)" in fields["근거"]
    assert fields["런북"] == "[api/unhandled-exception.md](https://github.com/o/OPS/blob/main/knowledge/api/unhandled-exception.md)"
    assert "INFRA PR #7" in fields["롤백 제안"]
    [button, link] = msg["components"][0]["components"]
    assert button["custom_id"] == "rollback:inc1", "버튼 값은 장애 ID뿐"
    assert link == {"type": 2, "style": 5, "url": PR.url, "label": "INFRA PR #7"}
    assert msg["allowed_mentions"] == {"parse": []}, "알림 문구에 @everyone이 있어도 멘션하지 않는다"


def test_card_without_rollback_has_no_button_but_says_why():
    msg = cards.incident_card("inc1", state("runbook"), None, "u", rollback_note="GitHub App 설정이 없다")
    assert "components" not in msg
    assert {"name": "롤백", "value": "GitHub App 설정이 없다", "inline": False} in msg["embeds"][0]["fields"]


def test_card_stays_inside_discord_limits():
    huge = "가" * 10_000
    s = state(root_cause={"root_cause": huge, "next_steps": [huge] * 5}, error=huge)
    s["incident"]["message"] = huge
    msg = cards.incident_card("i" * 80, s, PR, "u")
    embed = msg["embeds"][0]
    assert len(embed["title"]) <= 256 and len(embed["description"]) <= 4096
    assert all(len(f["value"]) <= 1024 and len(f["name"]) <= 256 for f in embed["fields"])
    assert sum(len(f["value"]) + len(f["name"]) for f in embed["fields"]) + len(embed["title"]) + len(embed["description"]) <= 6000
    assert all(len(c.get("label", "")) <= 80 for c in msg["components"][0]["components"])
    assert len(msg["content"]) <= 2000


def test_security_card_color_follows_the_worst_severity():
    red = cards.security_card("2026-11-25", {"trivy": {"CRITICAL": 1, "LOW": 3}}, 1, "s", ["CVE 처리"])
    green = cards.security_card("2026-11-25", {}, 0, "s", [])
    assert red["embeds"][0]["color"] == 0xE74C3C and green["embeds"][0]["color"] == 0x2ECC71
    assert {"name": "trivy", "value": "CRITICAL 1 · LOW 3", "inline": True} in red["embeds"][0]["fields"]


def test_client_posts_with_bot_token_and_replies_by_reference():
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["Authorization"], request.content))
        return httpx.Response(200, json={"id": "999"})

    http = httpx.Client(base_url="https://discord.com/api/v10", headers={"Authorization": "Bot t"},
                        transport=httpx.MockTransport(handle))
    client = DiscordClient("t", http)
    assert client.post("C1", {"content": "x"}, reply_to="55") == "999"
    client.edit("C1", "55", {"components": []})
    assert [(m, p, a) for m, p, a, _ in seen] == [("POST", "/api/v10/channels/C1/messages", "Bot t"),
                                                  ("PATCH", "/api/v10/channels/C1/messages/55", "Bot t")]
    assert b'"message_reference":{"message_id":"55"' in seen[0][3].replace(b" ", b"")


def test_client_failure_does_not_raise():
    http = httpx.Client(base_url="https://d", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert DiscordClient("t", http).post("C1", {"content": "x"}) == ""


def test_click_by_infra_merges_removes_the_button_and_replies_on_the_card(container, github):
    with_proposal(container, github)
    status, private = handle_click(container, "inc1", "U-ALICE", [INFRA_ROLE], "C-INC", "m1")
    assert (status, private) == ("merged", "")
    assert container.discord.edits == [("C-INC", "m1", {"components": []})]
    [(channel, msg, reply_to)] = container.discord.posts
    assert (channel, reply_to) == ("C-INC", "m1") and msg["content"].startswith("✅ 롤백 PR merge") and "<@U-ALICE>" in msg["content"]

    status, private = handle_click(container, "inc1", "U-BOB", [INFRA_ROLE], "C-INC", "m1")
    assert status == "already" and "이미 승인" in private and len(container.discord.posts) == 1


def test_click_by_someone_else_is_answered_privately(container, github):
    with_proposal(container, github)
    status, private = handle_click(container, "inc1", "U-DEV", ["R-FULLSTACK"], "C-INC", "m1")
    assert status == "not_allowed" and "승인 역할" in private
    assert container.discord.posts == [] and container.discord.edits == []


def test_window_edge_is_inclusive():
    from app.rollback import propose

    assert propose(incident(minutes_after=30), ["api"], timedelta(minutes=30)) == PLAN
