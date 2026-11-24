"""GitHub Actions 보안 스캔 결과를 S3에서 가져와 security DB에 넣는다(SPEC contracts/security-scan-upload.md).

러너는 클러스터 밖이라 ClusterIP api에 닿지 않는다. 그래서 CI는 도구 JSON을 S3에 올리고, 클러스터 안 CronJob이 가져온다.
이미 넣은 키는 security_scan_objects에 남아 있어 몇 번을 돌려도 결과가 같다.
실행: python -m app.security.ingest_s3
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from app.security.findings import Source, SecurityReportIn, parse
from app.security.store import SecurityStore

log = logging.getLogger("ingest_s3")

SCAN_REPOS = ("SERVICE-maemusae", "OPS-maemusae", "INFRA-maemusae")
SCAN_TOOLS = (Source.TRIVY, Source.GITLEAKS)
KEY_RE = re.compile(rf"^({'|'.join(SCAN_REPOS)})/({'|'.join(SCAN_TOOLS)})/(\d{{4}}-\d{{2}}-\d{{2}})/(\d+)\.json$")


@dataclass(frozen=True)
class ScanKey:
    repo: str
    tool: Source
    day: date
    run_id: str


def parse_key(key: str) -> ScanKey | None:
    m = KEY_RE.match(key)
    if not m:
        return None
    try:
        day = date.fromisoformat(m[3])
    except ValueError:
        return None
    return ScanKey(m[1], Source(m[2]), day, m[4])


@dataclass
class IngestReport:
    ingested: int = 0
    findings: int = 0
    rejected: dict[str, str] = field(default_factory=dict)


def list_keys(s3, bucket: str) -> list[str]:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket)
    return [o["Key"] for page in pages for o in page.get("Contents", [])]


def ingest(store: SecurityStore, s3, bucket: str, github_owner: str) -> IngestReport:
    """S3 읽기 실패는 그대로 올려 Job을 실패로 끝낸다. 다음 실행이 같은 키를 다시 읽는다."""
    report = IngestReport()
    keys = list_keys(s3, bucket)
    done = store.ingested(keys)
    for key in sorted(set(keys) - done):
        sk = parse_key(key)
        if sk is None:
            store.reject_object(key, "키가 <repo>/<tool>/<YYYY-MM-DD>/<run_id>.json 형식이 아니다")
            report.rejected[key] = "key"
            continue
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        try:
            findings = parse(sk.tool, SecurityReportIn(target=sk.repo, report=json.loads(body)))
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            store.reject_object(key, f"{sk.tool} JSON을 읽지 못했다: {type(e).__name__}: {str(e)[:300]}")
            report.rejected[key] = "body"
            continue
        run_url = f"https://github.com/{github_owner}/{sk.repo}/actions/runs/{sk.run_id}"
        if store.record_object(key, sk.tool, sk.repo, run_url, findings) is not None:
            report.ingested += 1
            report.findings += len(findings)
    return report


def main() -> None:
    import boto3

    from app.container import Container

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    c = Container()
    s = c.settings
    if not s.security_scan_bucket:
        raise SystemExit("SECURITY_SCAN_BUCKET이 없어 수집하지 않는다")
    # 자격은 기본 체인(EKS Pod Identity)에서 온다
    result = ingest(c.security, boto3.client("s3"), s.security_scan_bucket, s.infra_repo.split("/")[0])
    log.info("ingested %d objects, %d findings, rejected %s", result.ingested, result.findings, result.rejected or "none")


if __name__ == "__main__":
    main()
