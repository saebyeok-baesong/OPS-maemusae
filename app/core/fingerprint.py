"""에러 지문. 숫자, ID, 시간을 지운 뒤 해시를 만들어 반복 알림을 합치고 분류 결과를 캐시하는 키로 쓴다."""
import hashlib
import re

from app.incident import Incident

_PATTERNS = [
    # Deployment Pod 이름의 템플릿 해시와 접미사. 뒤에 한글 조사가 붙어 \b가 맞지 않으므로 영숫자가 아닌지로 끝을 본다
    (re.compile(r"-[bcdfghjklmnpqrstvwxz2456789]{6,10}-[bcdfghjklmnpqrstvwxz2456789]{5}(?![a-z0-9])"), "-<pod>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?\b"), "<ts>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hash>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\s+"), " "),
]


def normalize(text: str) -> str:
    out = text.strip()
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out.lower()


def _stack_frames(stacktrace: str | None, limit: int = 5) -> list[str]:
    """파일명과 함수명만. 줄 번호는 배포마다 바뀐다."""
    if not stacktrace:
        return []
    frames = re.findall(r'File "([^"]+)", line \d+, in (\S+)', stacktrace)
    return [f"{path.rsplit('/', 1)[-1]}:{func}" for path, func in frames[-limit:]]


def _last_line(logs: str | None) -> list[str]:
    """Pod 알림은 문구가 같아도 원인이 다를 수 있다. 마지막 로그 줄(대개 예외 한 줄)로 구분한다."""
    lines = [line for line in (logs or "").splitlines() if line.strip()]
    return [normalize(lines[-1])] if lines else []


def fingerprint(incident: Incident) -> str:
    first_line = incident.message.strip().splitlines()[0] if incident.message.strip() else ""
    ev = incident.evidence
    basis = "|".join([incident.app.lower(), normalize(first_line), *_stack_frames(ev.stacktrace), *_last_line(ev.pod_logs)])
    return hashlib.sha256(basis.encode()).hexdigest()[:24]
