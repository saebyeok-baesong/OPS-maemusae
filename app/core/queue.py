"""Redis Streams 큐.

API는 큐에 넣고 바로 202를 돌려준다. 분석은 수십 초가 걸려 Alertmanager webhook 시간 제한 안에 끝나지 않는다.
Consumer Group이라 워커를 늘려도 메시지는 한 워커에만 가고, 처리 중 죽은 워커의 메시지는 다른 워커가 회수한다.
"""
from collections.abc import Iterator

from pydantic import TypeAdapter
from redis import Redis
from redis.exceptions import ResponseError

from app.incident import QueueJob

_JOB = TypeAdapter(QueueJob)


def _str(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class IncidentQueue:
    def __init__(self, redis: Redis, stream: str, group: str, dlq_stream: str):
        self.redis = redis
        self.stream = stream
        self.group = group
        self.dlq_stream = dlq_stream

    def ensure_group(self) -> None:
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def enqueue(self, job: QueueJob) -> str:
        return _str(self.redis.xadd(self.stream, {"data": _JOB.dump_json(job)}))

    @staticmethod
    def _decode(messages) -> list[tuple[str, QueueJob]]:
        return [(_str(mid), _JOB.validate_json(fields.get(b"data") or fields.get("data"))) for mid, fields in messages]

    def consume(self, consumer: str, count: int = 1, block_ms: int = 5000) -> Iterator[tuple[str, QueueJob]]:
        self.ensure_group()
        resp = self.redis.xreadgroup(self.group, consumer, {self.stream: ">"}, count=count, block=block_ms)
        for _stream, messages in resp or []:
            yield from self._decode(messages)

    def reclaim_stale(self, consumer: str, min_idle_ms: int) -> list[tuple[str, QueueJob]]:
        """죽은 워커가 잡고 있던 메시지를 하나씩 회수한다. 여러 개를 잡으면 뒤의 것은 처리 전에 유휴 시간이 쌓여 다시 회수된다."""
        self.ensure_group()
        _next, messages, *_ = self.redis.xautoclaim(self.stream, self.group, consumer, min_idle_ms, "0-0", count=1)
        return self._decode(messages)

    def keep_claimed(self, consumer: str, msg_id: str) -> None:
        """처리 중인 메시지의 유휴 시간을 0으로 되돌린다. 다른 워커의 reclaim_stale은 유휴 시간만 본다."""
        self.redis.xclaim(self.stream, self.group, consumer, 0, [msg_id], justid=True)

    def ack(self, msg_id: str) -> None:
        self.redis.xack(self.stream, self.group, msg_id)

    def dead_letter(self, msg_id: str, job: QueueJob, error: str) -> None:
        self.redis.xadd(self.dlq_stream, {"data": _JOB.dump_json(job), "error": error[:2000]})
        self.ack(msg_id)

    def depth(self) -> dict[str, int]:
        self.ensure_group()
        group = next(g for g in self.redis.xinfo_groups(self.stream) if _str(g["name"]) == self.group)
        last = _str(group["last-delivered-id"])
        return {
            "waiting": len(self.redis.xrange(self.stream, min=f"({last}", max="+")),
            "in_progress": group["pending"],
            "dead_letters": self.redis.xlen(self.dlq_stream),
        }
