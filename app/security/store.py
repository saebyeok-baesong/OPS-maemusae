"""보안 결과와 일일 보고(security DB)."""
from datetime import date, datetime, timedelta

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.security.findings import Finding, Source

# 보고를 만들다 죽은 워커의 몫을 다른 워커가 가져가기까지 기다리는 시간
CLAIM_STALE = timedelta(minutes=30)


class SecurityStore:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def record_run(self, source: Source, target: str, run_url: str | None, findings: list[Finding]) -> int:
        with self.pool.connection() as conn, conn.transaction():
            return _insert_run(conn, source, target, run_url, findings)

    def ingested(self, keys: list[str]) -> set[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT key FROM security_scan_objects WHERE key = ANY(%s)", (keys,)).fetchall()
        return {r["key"] for r in rows}

    def record_object(self, key: str, source: Source, target: str, run_url: str, findings: list[Finding]) -> int | None:
        """객체 하나를 한 트랜잭션으로 넣는다. 다른 Job이 먼저 넣었으면 아무것도 하지 않고 None."""
        with self.pool.connection() as conn, conn.transaction():
            if not _claim_object(conn, key, None):
                return None
            run_id = _insert_run(conn, source, target, run_url, findings)
            conn.execute("UPDATE security_scan_objects SET run_id = %s WHERE key = %s", (run_id, key))
        return run_id

    def reject_object(self, key: str, error: str) -> None:
        """고쳐지지 않는 객체(키나 JSON 형식이 틀림)는 사유와 함께 기록해 다음 실행에서 다시 읽지 않는다."""
        with self.pool.connection() as conn:
            _claim_object(conn, key, error)

    def window(self, since: datetime) -> tuple[dict[str, dict[str, int]], list[dict]]:
        """since 이후에 보인 결과의 도구 x 심각도 집계와, since 이후 처음 나온 결과."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT source, severity, count(*) AS n FROM security_findings WHERE last_seen >= %s GROUP BY source, severity",
                (since,),
            ).fetchall()
            new = conn.execute(
                """SELECT source, target, rule_id, severity, title, location FROM security_findings WHERE first_seen >= %s
                   ORDER BY array_position(ARRAY['CRITICAL','HIGH','MEDIUM','LOW','UNKNOWN'], severity), first_seen""",
                (since,),
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for r in rows:
            counts.setdefault(r["source"], {})[r["severity"]] = r["n"]
        return counts, new

    def claim_report(self, day: date) -> bool:
        """그날 보고를 만들 권리. 워커가 여럿이어도 한 워커만 True를 받는다."""
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO security_reports (report_date, status, claimed_at) VALUES (%s, 'claimed', now())
                   ON CONFLICT (report_date) DO UPDATE SET claimed_at = now()
                   WHERE security_reports.status = 'claimed' AND security_reports.claimed_at < now() - %s
                   RETURNING report_date""",
                (day, CLAIM_STALE),
            ).fetchone()
        return row is not None

    def finish_report(self, day: date, counts: dict, new_count: int, summary: str, top_actions: list[str],
                      message_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                """UPDATE security_reports SET status = 'posted', counts = %s, new_count = %s, summary = %s,
                     top_actions = %s, message_id = %s, posted_at = now() WHERE report_date = %s""",
                (Jsonb(counts), new_count, summary, Jsonb(top_actions), message_id, day),
            )

    def report(self, day: date) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM security_reports WHERE report_date = %s", (day,)).fetchone()


def _claim_object(conn, key: str, error: str | None) -> bool:
    return conn.execute(
        "INSERT INTO security_scan_objects (key, error) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING RETURNING key",
        (key, error),
    ).fetchone() is not None


def _insert_run(conn, source: Source, target: str, run_url: str | None, findings: list[Finding]) -> int:
    run_id = conn.execute(
        "INSERT INTO security_runs (source, target, run_url, finding_count) VALUES (%s, %s, %s, %s) RETURNING id",
        (source, target, run_url, len(findings)),
    ).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO security_findings (key, source, target, rule_id, severity, title, location, last_run_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET severity = excluded.severity, title = excluded.title,
                 last_seen = now(), last_run_id = excluded.last_run_id""",
            [(f.key, f.source, f.target, f.rule_id, f.severity, f.title, f.location, run_id) for f in findings],
        )
    return run_id
