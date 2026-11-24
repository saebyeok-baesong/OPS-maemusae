"""테스트는 로컬 PostgreSQL(docker compose up -d postgres)만 쓴다. Redis, LLM, GitHub, Discord는 가짜다."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.config import Settings  # noqa: E402
from app.container import Container  # noqa: E402
from app.db import INCIDENT_TABLES, SECURITY_TABLES, ensure_database, migrate_all, truncate  # noqa: E402
from app.llm.fake import demo_responder, fake_model_factory  # noqa: E402
from tests.fakes import FakeDiscord, FakeGitHub, image, images_yaml  # noqa: E402

INCIDENT_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://ops:ops@localhost:55432/ops_test_incident")
SECURITY_DB = os.environ.get("TEST_SECURITY_DATABASE_URL", "postgresql://ops:ops@localhost:55432/ops_test_security")
TOKEN = {"Authorization": "Bearer am-token"}
SECURITY_TOKEN = {"Authorization": "Bearer sec-token"}
INFRA_ROLE = "R-INFRA"
FIXTURE = ROOT / "tests" / "fixtures" / "alertmanager-webhook.json"
# 픽스처 알림(api 예외)이 05:12:30에 시작됐다. api는 그 4분 전에 배포됐다
DEPLOYED_AT = datetime(2026, 11, 24, 5, 8, tzinfo=timezone.utc)


def settings(**overrides) -> Settings:
    return Settings(
        _env_file=None, alertmanager_token="am-token", security_report_token="sec-token",
        redis_url="fakeredis://", database_url=INCIDENT_DB, security_database_url=SECURITY_DB,
        embedding_provider="hash", light_model="fake:light", heavy_model="fake:heavy",
        model_prices={"fake:light": [1.0, 2.0], "fake:heavy": [10.0, 30.0]},
        discord_incident_channel_id="C-INC", discord_security_channel_id="C-SEC", discord_approver_role_ids=[INFRA_ROLE],
        **overrides,
    )


@pytest.fixture(scope="session")
def databases() -> None:
    ensure_database(INCIDENT_DB)
    ensure_database(SECURITY_DB)
    migrate_all(INCIDENT_DB, SECURITY_DB)


@pytest.fixture(scope="session")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


@pytest.fixture
def llm_calls() -> list:
    return []


@pytest.fixture
def github() -> FakeGitHub:
    """main의 images.yaml 이력: 어제 web, api 첫 배포 -> 픽스처 장애 4분 전에 api 새 이미지."""
    gh = FakeGitHub()
    yesterday = DEPLOYED_AT - timedelta(days=1)
    gh.commit("main", images_yaml(web=image("web", "w1"), api=image("api", "a1")), "chore: 첫 배포", yesterday)
    gh.commit("main", images_yaml(web=image("web", "w1"), api=image("api", "a2")), "deploy(api): a2 (#7)", DEPLOYED_AT)
    return gh


@pytest.fixture
def container(databases, llm_calls, github, private_key) -> Container:
    c = Container(settings(), model_factory=fake_model_factory(demo_responder, llm_calls))
    truncate(c.db, INCIDENT_TABLES)
    truncate(c.security_db, SECURITY_TABLES)
    c.__dict__.update(discord=FakeDiscord(), infra=github.repo(private_key), loki=None, kube=None)
    yield c
    c.db.close()
    c.security_db.close()


@pytest.fixture
def webhook() -> dict:
    return json.loads(FIXTURE.read_text())
