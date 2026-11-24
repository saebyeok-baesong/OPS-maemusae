"""큐 소비 워커(4단계 분석, 5단계 보고). 메시지 하나가 LangGraph 실행 한 번이다. 기다리는 틈에 보안 일일 보고를 챙긴다.

실행: python -m app.worker
"""
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app import rollback
from app.container import Container
from app.core.queue import IncidentQueue
from app.discord import cards
from app.graph.workflow import RECURSION_LIMIT
from app.incident import AnalyzeJob, QueueJob, ResolvedJob
from app.rag.loader import load_markdown_dir

log = logging.getLogger("worker")
KNOWLEDGE_DIR = "knowledge"


def run_graph(c: Container, job: AnalyzeJob) -> tuple[dict, list[str]]:
    """체크포인트가 있으면 남은 단계부터 잇는다. 두 번째 값은 이어서 시작한 단계(새로 시작하면 빈 목록)."""
    config = {"configurable": {"thread_id": job.incident_id}, "recursion_limit": RECURSION_LIMIT}
    snapshot = c.graph.get_state(config)
    if snapshot and snapshot.next:
        return c.graph.invoke(None, config), list(snapshot.next)
    if snapshot and snapshot.values.get("report"):
        # 그래프는 끝났고 저장이나 카드 게시 전에 워커가 죽었다
        return snapshot.values, ["(완료)"]
    initial = {"incident_id": job.incident_id, "incident": job.incident.model_dump(mode="json"), "fingerprint": job.fingerprint}
    return c.graph.invoke(initial, config), []


def plan_rollback(c: Container, job: AnalyzeJob, previous: dict) -> tuple[rollback.RollbackPR | None, str | None]:
    """(열린 PR, 카드에 남길 사유). PR 열기는 같은 branch 이름이라 다시 불러도 PR은 하나다."""
    s = c.settings
    plan = rollback.propose(job.incident, s.rollback_apps, timedelta(minutes=s.rollback_window_min))
    if plan is None:
        return None, None
    if raw := previous.get("rollback_pr"):
        return rollback.RollbackPR.model_validate(raw), None
    if c.infra is None:
        return None, "배포 직후 장애지만 GitHub App 설정이 없어 롤백 PR을 열지 않았다"
    try:
        return rollback.open_pr(c.infra, job.incident_id, job.incident, plan), None
    except Exception as e:  # noqa: BLE001 - PR을 못 열어도 분석 카드는 보낸다
        log.exception("rollback PR for %s failed", job.incident_id)
        return None, f"롤백 PR을 열지 못했다: {type(e).__name__}"


def process_analyze(c: Container, msg_id: str, job: AnalyzeJob) -> dict:
    channel = c.settings.discord_incident_channel_id
    c.store.set_status(job.incident_id, "running")
    try:
        final, resumed_from = run_graph(c, job)
    except Exception as e:  # noqa: BLE001 - 어떤 실패든 DLQ로 보내고 다음 메시지를 처리한다
        log.exception("incident %s failed", job.incident_id)
        c.store.set_status(job.incident_id, "failed", error=str(e))
        c.queue.dead_letter(msg_id, job, repr(e))
        state = {"incident": job.incident.model_dump(mode="json"), "outcome": "failed", "error": str(e)}
        c.discord.post(channel, cards.incident_card(job.incident_id, state, None, c.settings.runbook_base_url))
        return state

    record = c.store.get(job.incident_id)
    pr, note = plan_rollback(c, job, record.result)
    c.store.set_status(
        job.incident_id, final["outcome"], report=final["report"],
        classification=final.get("classification", {}), root_cause=final.get("root_cause"),
        context_doc_ids=[d["doc_id"] for d in final.get("context_docs", [])], cost=final.get("cost", {}),
        rollback_pr=pr.model_dump(mode="json") if pr else None, rollback_note=note, resumed_from=resumed_from,
    )
    # 다시 처리한 메시지가 카드를 두 번 올리지 않게 한다. 게시와 기록 사이에 죽으면 두 번 갈 수 있다
    if not record.card_message_id:
        message_id = c.discord.post(channel, cards.incident_card(job.incident_id, final, pr, c.settings.runbook_base_url, note))
        if message_id:
            c.store.set_card(job.incident_id, channel, message_id)
    c.queue.ack(msg_id)
    return {**final, "rollback_pr": pr, "rollback_note": note}


def process_resolved(c: Container, msg_id: str, job: ResolvedJob) -> str | None:
    """카드에 해소 확인을 단다. 승인된 롤백이 있었으면 조치부터 해소까지 걸린 시간을 같이 남긴다."""
    record = c.store.get(job.incident_id)
    text = None
    if record and record.card_message_id:
        minutes = (job.resolved_at - record.incident.occurred_at).total_seconds() / 60
        text = f"✅ 알림 해소 확인 · 발생 {minutes:.0f}분 뒤"
        merged = [a for a in c.store.actions(job.incident_id) if a["status"] == rollback.Status.MERGED and a["finished_at"]]
        if merged:
            after = (job.resolved_at - merged[0]["finished_at"]).total_seconds() / 60
            text += f" · 롤백 merge {after:.0f}분 뒤 (승인 <@{merged[0]['approver']}>)"
        c.discord.post(record.card_channel_id, cards.reply(text), reply_to=record.card_message_id)
    c.queue.ack(msg_id)
    return text


def process(c: Container, msg_id: str, job: QueueJob):
    match job:
        case AnalyzeJob():
            return process_analyze(c, msg_id, job)
        case ResolvedJob():
            return process_resolved(c, msg_id, job)


@contextmanager
def holding(queue: IncidentQueue, consumer: str, msg_id: str, claim_idle_ms: int) -> Iterator[None]:
    """처리가 회수 기준보다 길어져도 다른 워커가 같은 메시지를 가져가지 않게 붙잡아 둔다.
    없으면 두 워커가 같은 장애를 이어 처리하고 둘 다 카드를 올린다."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(claim_idle_ms / 3000):
            try:
                queue.keep_claimed(consumer, msg_id)
            except RedisError as e:
                log.warning("메시지 %s 붙잡기 실패: %s", msg_id, e)

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()


def run_forever() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    c = Container()
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    idle_ms = c.settings.queue_claim_idle_ms
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # 이미지에 든 런북을 증분 수집한다. 바뀐 청크만 임베딩하므로 재시작마다 돌아도 비용이 거의 없다
    report = c.ingestor.ingest(load_markdown_dir(KNOWLEDGE_DIR))
    log.info("worker %s started, knowledge %s", consumer, {k: v for k, v in report.to_dict().items() if k != "details"})

    while running:
        try:
            c.queue.ensure_group()
            for msg_id, job in c.queue.reclaim_stale(consumer, idle_ms):
                with holding(c.queue, consumer, msg_id, idle_ms):
                    process(c, msg_id, job)
            for msg_id, job in c.queue.consume(consumer, count=1, block_ms=5000):
                with holding(c.queue, consumer, msg_id, idle_ms):
                    process(c, msg_id, job)
            c.digest.tick(datetime.now(timezone.utc))
        except RedisConnectionError:
            # 처리 중이던 메시지는 pending으로 남아 다시 처리된다
            log.warning("redis(%s) 연결 실패, 3초 후 재시도", c.settings.redis_url)
            time.sleep(3)


if __name__ == "__main__":
    run_forever()
