from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import create_app
from app.security.digest import due_date
from app.security.findings import Severity, parse_gitleaks, parse_kube_bench, parse_trivy
from tests.conftest import SECURITY_TOKEN, TOKEN

TRIVY = {"ArtifactName": "123.dkr.ecr/api@sha256:aa", "Results": [{
    "Target": "app/requirements.txt",
    "Vulnerabilities": [{"VulnerabilityID": "CVE-2026-0001", "PkgName": "fastapi", "InstalledVersion": "0.110.0",
                         "FixedVersion": "0.115.1", "Severity": "HIGH", "Title": "DoS"}],
    "Misconfigurations": [{"ID": "DS002", "Title": "root user", "Severity": "MEDIUM"}],
    "Secrets": [{"RuleID": "aws-access-key-id", "Severity": "CRITICAL", "Title": "AWS Access Key", "StartLine": 3,
                 "Match": "AKIA****************"}],
}]}
SECRET = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
GITLEAKS = [{"RuleID": "github-pat", "Description": "GitHub Personal Access Token", "File": "backend/.env.sample",
             "StartLine": 4, "Commit": "0123456789abcdef", "Secret": SECRET, "Match": f"TOKEN={SECRET}", "Fingerprint": "x"}]
KUBE_BENCH = {"Controls": [{"id": "4", "node_type": "node", "tests": [{"section": "4.1", "results": [
    {"test_number": "4.1.1", "test_desc": "kubelet 서비스 파일 권한 600", "status": "FAIL"},
    {"test_number": "4.1.2", "test_desc": "소유자 root", "status": "PASS"},
    {"test_number": "4.2.6", "test_desc": "protect-kernel-defaults", "status": "WARN"},
]}]}]}


def test_parsers_map_each_tool_to_findings():
    trivy = parse_trivy("api", TRIVY)
    assert [(f.rule_id, f.severity) for f in trivy] == [("CVE-2026-0001", Severity.HIGH), ("DS002", Severity.MEDIUM),
                                                        ("aws-access-key-id", Severity.CRITICAL)]
    assert trivy[0].title == "fastapi 0.110.0 -> 0.115.1: DoS"
    [leak] = parse_gitleaks("SERVICE-watch-nyang", GITLEAKS)
    assert leak.location == "backend/.env.sample:4@0123456" and leak.severity == Severity.CRITICAL
    assert [(f.rule_id, f.severity) for f in parse_kube_bench("eks", KUBE_BENCH)] == [("4.1.1", Severity.HIGH), ("4.2.6", Severity.MEDIUM)]


def send(c, source, target, report):
    return TestClient(create_app(c), headers=SECURITY_TOKEN).post(f"/api/v1/security/{source}",
                                                                  json={"target": target, "report": report, "run_url": "https://ci/1"})


def test_secret_values_never_reach_the_database(container):
    assert send(container, "gitleaks", "SERVICE-watch-nyang", GITLEAKS).json()["findings"] == 1
    send(container, "trivy", "api", TRIVY)
    with container.security_db.connection() as conn:
        dump = str(conn.execute("SELECT * FROM security_findings").fetchall()) + str(conn.execute("SELECT * FROM security_runs").fetchall())
    assert SECRET not in dump and "AKIA" not in dump


def test_security_endpoint_auth_and_shape(container):
    client = TestClient(create_app(container))
    body = {"target": "x", "report": TRIVY}
    assert client.post("/api/v1/security/trivy", json=body, headers=TOKEN).status_code == 401, "장애 토큰으로 보안 결과를 넣을 수 없다"
    assert client.post("/api/v1/security/nmap", json=body, headers=SECURITY_TOKEN).status_code == 422
    assert client.post("/api/v1/security/gitleaks", json=body, headers=SECURITY_TOKEN).status_code == 422, "gitleaks는 배열"


def test_same_finding_twice_is_one_row(container):
    send(container, "trivy", "api", TRIVY)
    send(container, "trivy", "api", TRIVY)
    with container.security_db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM security_findings").fetchone()["n"] == 3
        assert conn.execute("SELECT count(*) AS n FROM security_runs").fetchone()["n"] == 2


def test_due_date_is_in_kst():
    assert due_date(datetime(2026, 11, 24, 23, 59, tzinfo=timezone.utc), 9) is None, "KST 08:59"
    assert str(due_date(datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc), 9)) == "2026-11-25"


NOW = datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc)  # KST 10:00


def test_daily_report_is_posted_once_by_one_worker(container, llm_calls):
    send(container, "trivy", "api", TRIVY)
    send(container, "kube-bench", "maemusae-eks", KUBE_BENCH)
    assert container.digest.tick(datetime(2026, 11, 24, 23, 0, tzinfo=timezone.utc)) is None, "보고 시각 전"

    assert str(container.digest.tick(NOW)) == "2026-11-25"
    assert container.digest.tick(NOW) is None, "같은 날 두 번째 tick"
    from app.security.digest import SecurityDigest

    other_worker = SecurityDigest(container.security, container.router, container.discord, "C-SEC", 9)
    assert other_worker.tick(NOW) is None, "다른 워커도 다시 만들지 않는다"

    [(channel, card, _)] = container.discord.posts
    fields = {f["name"]: f["value"] for f in card["embeds"][0]["fields"]}
    assert channel == "C-SEC" and fields["trivy"] == "CRITICAL 1 · HIGH 1 · MEDIUM 1" and fields["kube-bench"] == "HIGH 1 · MEDIUM 1"
    assert fields["오늘 처음 나온 결과"] == "5건"
    assert llm_calls == [("fake:light", "SecuritySummary")]
    report = container.security.report(NOW.date())
    assert report["status"] == "posted" and report["message_id"] == "m1"
    assert container.cost.summary("security:2026-11-25")["calls:fake:light"] == 1


def test_quiet_day_still_reports_without_llm(container, llm_calls):
    container.digest.tick(NOW)
    card = container.discord.posts[0][1]
    assert "들어온 보안 결과가 없다" in str(card) and llm_calls == []


def test_stale_claim_is_taken_over(container):
    day = NOW.date()
    assert container.security.claim_report(day)
    assert not container.security.claim_report(day)
    with container.security_db.connection() as conn:
        conn.execute("UPDATE security_reports SET claimed_at = now() - interval '31 minutes'")
    assert container.security.claim_report(day), "만들다 죽은 워커의 몫은 30분 뒤 다른 워커가 가져간다"
