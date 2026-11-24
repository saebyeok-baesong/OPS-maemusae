"""보안 도구 결과를 하나의 모양(Finding)으로 바꾼다. 도구마다 파서 하나, 표 하나가 도구를 고른다.

비밀값은 받는 즉시 버린다. gitleaks의 Secret, Match와 Trivy secret의 Match는 Finding에 옮기지 않는다.
"""
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class Source(StrEnum):
    TRIVY = "trivy"
    GITLEAKS = "gitleaks"
    KUBE_BENCH = "kube-bench"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Finding:
    source: Source
    target: str          # 저장소, 이미지, 클러스터 이름
    rule_id: str
    severity: Severity
    title: str
    location: str

    @property
    def key(self) -> str:
        """같은 문제가 매일 다시 와도 한 줄이다. 처음 본 날로 '새 결과'를 가린다."""
        return hashlib.sha256("|".join([self.source, self.target, self.rule_id, self.location]).encode()).hexdigest()[:32]


class SecurityReportIn(BaseModel):
    """kube-bench Job이 api로 보내는 본문. S3 수집도 같은 모양으로 감싸 파서에 넘긴다. report는 도구의 JSON 출력 그대로."""

    target: str
    run_url: str | None = None
    report: dict | list


def _severity(value: Any) -> Severity:
    try:
        return Severity(str(value).upper())
    except ValueError:
        return Severity.UNKNOWN


def parse_trivy(target: str, report: dict) -> list[Finding]:
    out = []
    for result in report.get("Results") or []:
        where = result.get("Target", "")
        for v in result.get("Vulnerabilities") or []:
            fixed = v.get("FixedVersion") or "수정 버전 없음"
            title = f"{v.get('PkgName')} {v.get('InstalledVersion')} -> {fixed}: {v.get('Title', '')}".strip()
            out.append(Finding(Source.TRIVY, target, v["VulnerabilityID"], _severity(v.get("Severity")), title[:300], where))
        for m in result.get("Misconfigurations") or []:
            out.append(Finding(Source.TRIVY, target, m.get("ID") or m.get("AVDID", "?"), _severity(m.get("Severity")),
                               m.get("Title", "")[:300], where))
        for s in result.get("Secrets") or []:
            out.append(Finding(Source.TRIVY, target, s.get("RuleID", "secret"), _severity(s.get("Severity")),
                               s.get("Title", "")[:300], f"{where}:{s.get('StartLine', '?')}"))
    return out


def parse_gitleaks(target: str, report: list) -> list[Finding]:
    # 새 비밀이 커밋에 들어갔다는 것 자체가 즉시 교체할 일이라 모두 CRITICAL로 본다
    return [Finding(Source.GITLEAKS, target, leak.get("RuleID", "?"), Severity.CRITICAL,
                    (leak.get("Description") or leak.get("RuleID", ""))[:300],
                    f"{leak.get('File', '?')}:{leak.get('StartLine', '?')}@{str(leak.get('Commit', ''))[:7]}")
            for leak in report]


KUBE_BENCH_SEVERITY = {"FAIL": Severity.HIGH, "WARN": Severity.MEDIUM}


def parse_kube_bench(target: str, report: dict) -> list[Finding]:
    """kube-bench --json. PASS, INFO는 버린다."""
    out = []
    for control in report.get("Controls") or []:
        for section in control.get("tests") or []:
            for r in section.get("results") or []:
                if sev := KUBE_BENCH_SEVERITY.get(r.get("status", "")):
                    out.append(Finding(Source.KUBE_BENCH, target, str(r.get("test_number", "?")), sev,
                                       r.get("test_desc", "")[:300], control.get("node_type", "")))
    return out


PARSERS: dict[Source, Callable[[str, Any], list[Finding]]] = {
    Source.TRIVY: parse_trivy,
    Source.GITLEAKS: parse_gitleaks,
    Source.KUBE_BENCH: parse_kube_bench,
}
EXPECTED_SHAPE: dict[Source, type] = {Source.TRIVY: dict, Source.GITLEAKS: list, Source.KUBE_BENCH: dict}


def parse(source: Source, body: SecurityReportIn) -> list[Finding]:
    if not isinstance(body.report, EXPECTED_SHAPE[source]):
        raise ValueError(f"{source} report must be a JSON {EXPECTED_SHAPE[source].__name__}")
    return PARSERS[source](body.target, body.report)
