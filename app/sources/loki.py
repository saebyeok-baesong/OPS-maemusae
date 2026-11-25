"""Loki에서 장애 근거를 꺼낸다. WatchNyang api의 JSON 로그(backend/app/core/logging.py)를 그대로 쓴다.

처리하지 못한 예외는 logger=app.error 줄에 exc_info와 request_id가 실린다. Pod 알림은 그 Pod의 마지막 로그 줄을 쓴다.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

log = logging.getLogger(__name__)
LOOKBACK = timedelta(minutes=5)


@dataclass(frozen=True)
class LoggedException:
    stacktrace: str
    request_id: str | None


class LokiClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(base_url=base_url, timeout=5, transport=transport)

    def _lines(self, query: str, since: datetime, until: datetime, limit: int) -> list[str] | None:
        """최신 줄부터. 조회에 실패해도 접수는 한다. 분류와 런북 안내는 알림 문구만으로도 된다."""
        params = {"query": query, "start": int((since - LOOKBACK).timestamp() * 1e9),
                  "end": int(until.timestamp() * 1e9), "limit": limit, "direction": "backward"}
        try:
            resp = self.http.get("/loki/api/v1/query_range", params=params)
            resp.raise_for_status()
            streams = resp.json()["data"]["result"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("loki lookup failed for %s: %s", query, e)
            return None
        values = sorted((v for s in streams for v in s["values"]), key=lambda v: int(v[0]), reverse=True)
        return [line for _ts, line in values]

    def latest_exception(self, namespace: str, container: str, since: datetime, until: datetime) -> LoggedException | None:
        query = f'{{namespace="{namespace}", container="{container}"}} | json | logger="app.error"'
        for line in self._lines(query, since, until, 5) or []:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if exc := data.get("exc_info"):
                rid = data.get("request_id")
                return LoggedException(exc, rid if rid and rid != "-" else None)
        return None

    def pod_tail(self, namespace: str, pod: str, since: datetime, until: datetime, limit: int = 40) -> str | None:
        """CrashLoop, OOMKilled 알림은 문구가 한 줄뿐이다. 죽기 직전 로그가 분류 근거가 된다."""
        lines = self._lines(f'{{namespace="{namespace}", pod="{pod}"}}', since, until, limit)
        return "".join(line if line.endswith("\n") else line + "\n" for line in reversed(lines)) if lines else None
