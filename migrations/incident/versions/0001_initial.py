"""incident DB 첫 스키마. 장애, 런북 벡터, LLM 호출, 승인 후 조치.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE incidents (
  id                text PRIMARY KEY,
  fingerprint       text NOT NULL,
  alertname         text NOT NULL,
  alert_fingerprint text NOT NULL,
  app               text NOT NULL,
  namespace         text NOT NULL,
  severity          text,
  message           text NOT NULL,
  incident          jsonb NOT NULL,
  status            text NOT NULL,
  category          text,
  needs_code_fix    boolean,
  report            text,
  result            jsonb NOT NULL DEFAULT '{}',
  card_channel_id   text,
  card_message_id   text,
  occurred_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  alert_resolved_at timestamptz
);
CREATE INDEX ON incidents (fingerprint, created_at DESC);
CREATE INDEX ON incidents (alert_fingerprint) WHERE alert_resolved_at IS NULL;

CREATE TABLE documents (
  id           bigserial PRIMARY KEY,
  doc_id       text NOT NULL,
  chunk_hash   text NOT NULL,
  content      text NOT NULL,
  service      text NOT NULL,
  category     text NOT NULL,
  doc_type     text NOT NULL,
  updated_at   timestamptz NOT NULL,
  embed_model  text NOT NULL,
  embedding    vector(384) NOT NULL,
  UNIQUE (doc_id, chunk_hash)
);
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON documents USING gin (content gin_trgm_ops);

CREATE TABLE actions (
  id          bigserial PRIMARY KEY,
  incident_id text NOT NULL REFERENCES incidents(id),
  action      text NOT NULL,
  target      text NOT NULL,
  approver    text NOT NULL,
  approved_at timestamptz NOT NULL DEFAULT now(),
  status      text NOT NULL,
  detail      text,
  finished_at timestamptz,
  UNIQUE (incident_id, action)
);

-- subject: 장애 ID 또는 security:YYYY-MM-DD. 장애 외의 호출도 같은 표에 쌓아 비용을 한 곳에서 본다
CREATE TABLE llm_calls (
  id          bigserial PRIMARY KEY,
  subject     text NOT NULL,
  task        text NOT NULL,
  model       text NOT NULL,
  tokens_in   int,
  tokens_out  int,
  usd         numeric(10, 6),
  cache_hit   boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON llm_calls (subject);
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute("DROP TABLE llm_calls, actions, documents, incidents")
