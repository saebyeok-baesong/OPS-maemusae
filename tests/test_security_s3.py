"""CI가 S3에 올린 스캔 결과를 클러스터 안 Job이 가져온다. S3는 moto로 바꿔 끼워 boto3 호출을 그대로 지나간다."""
import json

import boto3
import pytest
from moto import mock_aws

from app.security.findings import Severity
from app.security.ingest_s3 import ingest, parse_key
from tests.test_security import GITLEAKS, SECRET, TRIVY

BUCKET = "maemusae-security-scans-123456789012"


@pytest.fixture
def s3(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="ap-northeast-2")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-2"})
        yield client


def upload(s3, key: str, body) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=body if isinstance(body, bytes) else json.dumps(body).encode())


def rows(c, sql: str) -> list[dict]:
    with c.security_db.connection() as conn:
        return conn.execute(sql).fetchall()


def test_key_contract():
    k = parse_key("SERVICE-maemusae/gitleaks/2026-11-25/1234567890.json")
    assert (k.repo, k.tool, k.day.isoformat(), k.run_id) == ("SERVICE-maemusae", "gitleaks", "2026-11-25", "1234567890")
    for bad in ("SERVICE-maemusae/kube-bench/2026-11-25/1.json", "OTHER/trivy/2026-11-25/1.json",
                "OPS-maemusae/trivy/2026-13-01/1.json", "OPS-maemusae/trivy/2026-11-25/1.sarif"):
        assert parse_key(bad) is None, bad


def test_uploaded_scans_are_ingested_once(container, s3):
    upload(s3, "SERVICE-maemusae/gitleaks/2026-11-25/111.json", GITLEAKS)
    upload(s3, "OPS-maemusae/trivy/2026-11-25/222.json", TRIVY)

    first = ingest(container.security, s3, BUCKET, "saebyeok-baesong")
    again = ingest(container.security, s3, BUCKET, "saebyeok-baesong")

    assert (first.ingested, first.findings, first.rejected) == (2, 4, {})
    assert (again.ingested, again.findings) == (0, 0), "이미 넣은 키는 다시 넣지 않는다"
    runs = rows(container, "SELECT source, target, run_url, finding_count FROM security_runs ORDER BY id")
    assert runs == [
        {"source": "trivy", "target": "OPS-maemusae", "finding_count": 3,
         "run_url": "https://github.com/saebyeok-baesong/OPS-maemusae/actions/runs/222"},
        {"source": "gitleaks", "target": "SERVICE-maemusae", "finding_count": 1,
         "run_url": "https://github.com/saebyeok-baesong/SERVICE-maemusae/actions/runs/111"},
    ]
    [leak] = rows(container, "SELECT severity FROM security_findings WHERE source = 'gitleaks'")
    assert leak["severity"] == Severity.CRITICAL, "비밀번호 유출은 최고 심각도"
    assert SECRET not in str(rows(container, "SELECT * FROM security_findings"))


def test_new_object_is_picked_up_on_the_next_run(container, s3):
    upload(s3, "SERVICE-maemusae/gitleaks/2026-11-25/111.json", GITLEAKS)
    ingest(container.security, s3, BUCKET, "o")
    upload(s3, "INFRA-maemusae/trivy/2026-11-26/333.json", TRIVY)
    assert ingest(container.security, s3, BUCKET, "o").ingested == 1


def test_broken_objects_are_recorded_and_not_retried(container, s3):
    upload(s3, "SERVICE-maemusae/trivy/2026-11-25/1.json", b"not json")
    upload(s3, "SERVICE-maemusae/gitleaks/2026-11-25/2.json", {"not": "a list"})
    upload(s3, "somewhere/else.json", GITLEAKS)

    first = ingest(container.security, s3, BUCKET, "o")
    assert first.ingested == 0 and set(first.rejected) == {
        "SERVICE-maemusae/trivy/2026-11-25/1.json", "SERVICE-maemusae/gitleaks/2026-11-25/2.json", "somewhere/else.json"}
    assert ingest(container.security, s3, BUCKET, "o").rejected == {}
    errors = rows(container, "SELECT key, error FROM security_scan_objects WHERE run_id IS NULL")
    assert len(errors) == 3 and all(e["error"] for e in errors)
    assert rows(container, "SELECT count(*) AS n FROM security_runs") == [{"n": 0}]


def test_two_jobs_racing_on_one_key_store_it_once(container):
    from app.security.findings import parse_gitleaks

    findings = parse_gitleaks("SERVICE-maemusae", GITLEAKS)
    key = "SERVICE-maemusae/gitleaks/2026-11-25/111.json"
    assert container.security.record_object(key, "gitleaks", "SERVICE-maemusae", "u", findings) is not None
    assert container.security.record_object(key, "gitleaks", "SERVICE-maemusae", "u", findings) is None
    assert rows(container, "SELECT count(*) AS n FROM security_runs") == [{"n": 1}]
