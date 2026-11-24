"""PostgreSQL 연결과 스키마. incident DB와 security DB는 서로 다른 RDS라 마이그레이션도 따로 올린다.

실행: python -m app.db (두 DB 모두 최신으로, LangGraph 체크포인트 테이블 포함)
"""
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = {"incident": ROOT / "migrations" / "incident", "security": ROOT / "migrations" / "security"}
# checkpoint_migrations는 버전 기록이라 비우지 않는다
INCIDENT_TABLES = ("llm_calls", "actions", "documents", "incidents", "checkpoints", "checkpoint_blobs", "checkpoint_writes")
SECURITY_TABLES = ("security_reports", "security_findings", "security_runs")


def sqlalchemy_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def make_pool(url: str) -> ConnectionPool:
    return ConnectionPool(url, min_size=1, max_size=5, kwargs={"row_factory": dict_row, "autocommit": True}, open=True)


def checkpointer(pool: ConnectionPool):
    from langgraph.checkpoint.postgres import PostgresSaver

    return PostgresSaver(pool)


def migrate(name: str, url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS[name]))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    if name == "incident":
        with make_pool(url) as pool:
            checkpointer(pool).setup()


def migrate_all(incident_url: str, security_url: str) -> None:
    migrate("incident", incident_url)
    migrate("security", security_url)


def ensure_database(url: str) -> None:
    """테스트용 데이터베이스가 없으면 만든다. 같은 서버의 postgres 데이터베이스로 접속해서 만든다."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = conninfo_to_dict(url)["dbname"]
    with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            conn.execute(f'CREATE DATABASE "{name}"')


def truncate(pool: ConnectionPool, tables: tuple[str, ...]) -> None:
    with pool.connection() as conn:
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")


if __name__ == "__main__":
    from app.config import get_settings

    s = get_settings()
    migrate_all(s.database_url, s.security_database_url)
