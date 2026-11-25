"""S3에서 가져온 스캔 결과 객체. 같은 객체를 두 번 넣지 않도록 키를 기록한다.

Revision ID: s0002
Revises: s0001
"""
from alembic import op

revision = "s0002"
down_revision = "s0001"


def upgrade() -> None:
    op.execute("""
CREATE TABLE security_scan_objects (
  key         text PRIMARY KEY,
  run_id      bigint REFERENCES security_runs(id),
  error       text,
  ingested_at timestamptz NOT NULL DEFAULT now()
);
""")


def downgrade() -> None:
    op.execute("DROP TABLE security_scan_objects")
