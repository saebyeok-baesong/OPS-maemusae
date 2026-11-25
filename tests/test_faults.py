"""장애 주입 테스트. verify/incident-pipeline/IncidentPipeline.tla 가 찾은 순서를 실제 코드로 다시 일으킨다.

테스트마다 docstring 에 주입한 장애, 재현하는 장애, 모델의 검사 이름(S, F, K)을 적는다.
전체 목록과 설명은 verify/incident-pipeline/README.md 에 있다.
xfail(strict=True)는 지금 코드에서 깨지는 약속이다. 코드를 고치면 XPASS 로 실패하니 그때 xfail 을 지우고
README.md 와 모델 기대값(run.sh CHECKS)을 같이 바꾼다.
"""
import threading

import httpx
import pytest

from app.rollback import Status, approve
from app.worker import process
from tests.conftest import INFRA_ROLE
from tests.test_pipeline import WorkerKilled, evidence, post  # noqa: F401 - evidence 는 fixture
from tests.test_rollback import with_proposal


def kill_once(obj, name: str, monkeypatch, *, nth: int = 1, after: bool = False):
    """obj.name 의 nth 번째 호출에서 워커를 죽인다(kill -9 처럼 except Exception 에 잡히지 않는다).
    after=True 면 호출이 끝난 뒤, 돌려주기 전에 죽인다."""
    original = getattr(obj, name)
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == nth and not after:
            raise WorkerKilled(name)
        result = original(*args, **kwargs)
        if calls["n"] == nth and after:
            raise WorkerKilled(name)
        return result

    monkeypatch.setattr(obj, name, wrapper)
    return original


def fail_open_pr(c, monkeypatch):
    def broken(*_a, **_k):
        raise httpx.HTTPStatusError("502 from GitHub", request=httpx.Request("POST", "https://api.github.com"),
                                    response=httpx.Response(502))
    monkeypatch.setattr(c.infra, "open_rollback_pr", broken)


def first_delivery(c):
    [(msg_id, job)] = list(c.queue.consume("w1", count=1, block_ms=10))
    return msg_id, job


def redeliver(c):
    """다른 워커(w2)가 죽은 워커의 메시지를 회수한다. 운영에서는 queue_claim_idle_ms(15분) 뒤다."""
    [(msg_id, job)] = c.queue.reclaim_stale("w2", min_idle_ms=0)
    return msg_id, job


def cards_with_button(c) -> list[dict]:
    return [m for _ch, m, reply in c.discord.posts if reply is None and m.get("components")]


def incident_cards(c) -> list[dict]:
    return [m for _ch, m, reply in c.discord.posts if reply is None]


# ---------------------------------------------------------------------------
# S1: 워커가 어디서 죽어도 장애 알림은 하나, 롤백 버튼은 동작한다

CRASH_POINTS = {
    # 이름: (주입 대상, 호출 순번, 호출 뒤에 죽는지)
    "그래프 끝, 기록 읽기 전": ("store", "get", 1, False),
    "롤백 PR을 열고 기록 전": ("infra", "open_rollback_pr", 1, True),
    "결과 저장 직전": ("store", "set_status", 2, False),
    "알림 게시와 기록 뒤, XACK 전": ("queue", "ack", 1, False),
}


@pytest.mark.parametrize("point", CRASH_POINTS)
def test_S1_worker_killed_anywhere_still_one_card_with_a_working_button(evidence, webhook, github, monkeypatch, point):
    """주입: 워커가 process_analyze 의 외부 호출 사이에서 죽고(kill -9, OOM) 다른 워커가 메시지를 회수한다.
    재현: 배포 직후 api 예외 장애를 분석하던 워커가 재시작된다.
    지킬 것: 장애 알림은 한 번만 올라가고, 그 알림의 롤백 버튼을 누르면 merge된다. 모델 S1(CardOnce, ButtonWorks)."""
    c = evidence
    [incident_id] = post(c, webhook, 0)["accepted"]
    target, name, nth, after = CRASH_POINTS[point]
    kill_once(getattr(c, target), name, monkeypatch, nth=nth, after=after)
    msg_id, job = first_delivery(c)
    with pytest.raises(WorkerKilled):
        process(c, msg_id, job)
    monkeypatch.undo()

    process(c, *redeliver(c))

    assert len(incident_cards(c)) == 1 and len(cards_with_button(c)) == 1
    assert len(github.pulls) == 1
    assert c.queue.reclaim_stale("w3", min_idle_ms=0) == [], "메시지는 XACK 되었다"
    assert approve(c.store, c.infra, incident_id, "U-ALICE", [INFRA_ROLE], [INFRA_ROLE]).status == Status.MERGED


def test_S1_redelivery_after_ack_crash_keeps_the_pr_even_if_github_fails(evidence, webhook, github, monkeypatch):
    """주입: 알림 게시 뒤 XACK 직전에 워커가 죽고, 다시 처리할 때 GitHub가 502를 준다.
    재현: 이미 올린 알림의 롤백 버튼이 다시 처리 때문에 죽는 일.
    지킬 것: 기록에 있는 rollback_pr 을 다시 쓰므로 GitHub 를 부르지 않고 버튼이 그대로 동작한다. 모델 S1(ButtonWorks), 변형 M2."""
    c = evidence
    [incident_id] = post(c, webhook, 0)["accepted"]
    kill_once(c.queue, "ack", monkeypatch)
    msg_id, job = first_delivery(c)
    with pytest.raises(WorkerKilled):
        process(c, msg_id, job)
    monkeypatch.undo()
    fail_open_pr(c, monkeypatch)

    process(c, *redeliver(c))

    assert c.store.get(incident_id).result["rollback_pr"] is not None
    assert len(incident_cards(c)) == 1
    monkeypatch.undo()
    assert approve(c.store, c.infra, incident_id, "U-ALICE", [INFRA_ROLE], [INFRA_ROLE]).status == Status.MERGED


# ---------------------------------------------------------------------------
# S2: 두 사람이 동시에 눌러도 롤백 실행은 한 번

def test_S2_two_approvers_at_the_same_moment_merge_once(container, github):
    """주입: 승인 역할을 가진 두 사람이 같은 순간에 승인 버튼을 누른다(스레드 두 개, 동시에 approve).
    재현: 장애 채널에서 인프라팀과 풀스택이 같이 버튼을 누르는 일.
    지킬 것: actions (incident_id, action) unique 제약으로 _merge 는 한 번만 돈다. 모델 S2(ExecuteOnce), 변형 M3."""
    with_proposal(container, github)
    barrier, results = threading.Barrier(2), []

    def click(user):
        barrier.wait()
        results.append(approve(container.store, container.infra, "inc1", user, [INFRA_ROLE], [INFRA_ROLE]).status)

    threads = [threading.Thread(target=click, args=(u,)) for u in ("U-ALICE", "U-BOB")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == sorted([Status.MERGED, Status.ALREADY])
    assert [m for m, p in github.calls if p.endswith("/merge")] == ["PUT"]


# ---------------------------------------------------------------------------
# 지금 코드에서 깨지는 약속(발견). 모델 F1~F4, K1

@pytest.mark.parametrize("point", ["discord.post", "queue.dead_letter"])
def test_F1_failed_analysis_and_worker_killed_still_posts_one_failure_card(evidence, webhook, monkeypatch, point):
    """주입: 분석 그래프가 예외를 내고(LLM 5xx 등), 워커가 실패 경로에서 죽는다(실패 알림 게시 전, 또는 게시 뒤 DLQ 전).
    재현: 고치기 전에는 dead_letter 가 XACK 한 뒤에 알림을 올려서, 그 사이에 죽으면 장애가 Discord 에 아무것도 남기지 않았다.
    지킬 것: 메시지가 DLQ 로 가기 전에 실패 알림이 꼭 한 번 올라간다. 모델 F1(NoSilentDrop), 변형 M4."""
    from app.graph.nodes import Nodes

    def llm_down(self, state):
        raise RuntimeError("LLM 503")

    c = evidence
    monkeypatch.setattr(Nodes, "classify", llm_down)
    c.__dict__.pop("graph", None)
    post(c, webhook, 0)
    target, name = point.split(".")
    original = kill_once(getattr(c, target), name, monkeypatch)
    msg_id, job = first_delivery(c)
    with pytest.raises(WorkerKilled):
        process(c, msg_id, job)
    monkeypatch.setattr(getattr(c, target), name, original)

    process(c, *redeliver(c))

    assert len(incident_cards(c)) == 1, f"장애 알림 {len(incident_cards(c))}개"
    assert c.queue.reclaim_stale("w3", min_idle_ms=0) == [] and c.queue.depth()["dead_letters"] == 1


@pytest.mark.xfail(strict=True, reason="F2: 두 워커가 같은 메시지를 처리하면 둘 다 card_message_id 가 없다고 본다")
def test_F2_heartbeat_lost_two_workers_post_two_cards(evidence, webhook, monkeypatch):
    """주입: 처리 중인 워커(w1)의 holding() XCLAIM 이 queue_claim_idle_ms 넘게 실패한다(그 워커만 Redis 와 끊김).
    w1 은 계속 돌고, w2 가 메시지를 회수해 같이 처리한다. 여기서는 w1 이 알림을 올리는 순간 w2 가 끝까지 처리하게 끼워 넣는다.
    재현: 장애 채널에 같은 장애 알림이 두 개 올라간다(롤백 버튼도 두 개).
    지킬 것: 장애 알림은 한 번. 모델 F2(CardOnce)."""
    c = evidence
    post(c, webhook, 0)
    real_post, reentered = c.discord.post, {"done": False}

    def post_while_w2_runs(channel, message, reply_to=None):
        if not reentered["done"]:
            reentered["done"] = True
            process(c, *redeliver(c))
        return real_post(channel, message, reply_to)

    monkeypatch.setattr(c.discord, "post", post_while_w2_runs)
    process(c, *first_delivery(c))

    assert len(incident_cards(c)) == 1, f"장애 알림 {len(incident_cards(c))}개"


@pytest.mark.xfail(strict=True, reason="F3: 늦게 끝난 워커가 rollback_pr 을 None 으로 덮어쓴다")
def test_F3_heartbeat_lost_and_github_error_break_the_rollback_button(evidence, webhook, github, monkeypatch):
    """주입: F2 처럼 두 워커가 같은 메시지를 처리하고, 늦게 끝나는 워커(w2)의 open_rollback_pr 이 GitHub 502 로 실패한다.
    순서: w2 기록 읽기(PR 없음) -> w1 PR 열기, 저장, 버튼 알림 게시 -> w2 PR 실패, rollback_pr=None 저장.
    재현: 장애 채널의 롤백 버튼을 누르면 "이 장애에는 실행할 수 있는 조치가 없습니다"가 나오고 롤백이 안 된다.
    지킬 것: 올라간 롤백 버튼은 merge 된다. 모델 F3(ButtonWorks)."""
    c = evidence
    [incident_id] = post(c, webhook, 0)["accepted"]
    w1_msg = first_delivery(c)
    w2_msg = redeliver(c)
    real_open, w2_waiting, w1_done = c.infra.open_rollback_pr, threading.Event(), threading.Event()

    def open_pr(*args, **kwargs):
        if threading.current_thread().name == "w2":
            w2_waiting.set()
            w1_done.wait(30)
            raise httpx.HTTPStatusError("502 from GitHub", request=httpx.Request("POST", "https://api.github.com"),
                                        response=httpx.Response(502))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(c.infra, "open_rollback_pr", open_pr)
    w2 = threading.Thread(target=process, args=(c, *w2_msg), name="w2")
    w2.start()
    assert w2_waiting.wait(30)
    process(c, *w1_msg)
    w1_done.set()
    w2.join(30)
    monkeypatch.undo()

    assert cards_with_button(c), "w1 이 롤백 버튼을 올렸다"
    assert approve(c.store, c.infra, incident_id, "U-ALICE", [INFRA_ROLE], [INFRA_ROLE]).status == Status.MERGED


@pytest.mark.xfail(strict=True, reason="F4: start_action 뒤 죽으면 actions 가 running 으로 남아 다시 승인할 수 없다")
def test_F4_approvals_killed_mid_merge_leaves_rollback_stuck(container, github, monkeypatch):
    """주입: 승인 봇(approvals)이 start_action 뒤, merge 전에 죽는다(Pod 재시작, OOM).
    재현: 승인한 사람이 다시 눌러도 "이미 승인되어 실행했거나 실행 중인 조치입니다"만 나오고 롤백은 merge 되지 않는다.
    지킬 것: 끝나지 않은 승인은 다시 할 수 있다. 모델 F4(NoStuckApproval)."""
    with_proposal(container, github)
    kill_once(container.infra, "current_images", monkeypatch)
    with pytest.raises(WorkerKilled):
        approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    monkeypatch.undo()

    again = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    assert again.status == Status.MERGED, f"다시 승인한 결과 {again.status}, actions {container.store.actions('inc1')}"


@pytest.mark.xfail(strict=True, reason="K1: 게시와 set_card 사이에 죽으면 두 번 간다. worker.py 주석이 받아들인 틈")
def test_K1_killed_between_post_and_set_card_posts_twice(evidence, webhook, monkeypatch):
    """주입: 워커가 discord.post 성공 뒤, store.set_card 전에 죽는다.
    재현: 같은 장애 알림이 두 번 올라간다. README 는 "메시지는 한 번만 올라갑니다"라고 하지만 코드 주석이 이 틈을 받아들였다.
    지킬 것: 장애 알림은 한 번. 모델 K1(CardOnce, CrashAfterPost)."""
    c = evidence
    post(c, webhook, 0)
    kill_once(c.store, "set_card", monkeypatch)
    msg_id, job = first_delivery(c)
    with pytest.raises(WorkerKilled):
        process(c, msg_id, job)
    monkeypatch.undo()

    process(c, *redeliver(c))

    assert len(incident_cards(c)) == 1, f"장애 알림 {len(incident_cards(c))}개"
