"""security DB 첫 스키마. 스캔 실행, 결과(비밀값 없음), 일일 보고.

Revision ID: s0001
Revises:
"""
from alembic import op

revision = "s0001"
down_revision = None

SCHEMA = """
CREATE TABLE security_runs (
  id            bigserial PRIMARY KEY,
  source        text NOT NULL,
  target        text NOT NULL,
  run_url       text,
  finding_count int NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE security_findings (
  id          bigserial PRIMARY KEY,
  key         text NOT NULL UNIQUE,
  source      text NOT NULL,
  target      text NOT NULL,
  rule_id     text NOT NULL,
  severity    text NOT NULL,
  title       text NOT NULL,
  location    text NOT NULL,
  first_seen  timestamptz NOT NULL DEFAULT now(),
  last_seen   timestamptz NOT NULL DEFAULT now(),
  last_run_id bigint REFERENCES security_runs(id)
);
CREATE INDEX ON security_findings (last_seen);
CREATE INDEX ON security_findings (first_seen);

CREATE TABLE security_reports (
  report_date date PRIMARY KEY,
  status      text NOT NULL,
  claimed_at  timestamptz NOT NULL,
  counts      jsonb,
  new_count   int,
  summary     text,
  top_actions jsonb,
  message_id  text,
  posted_at   timestamptz
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE security_reports, security_findings, security_runs")
