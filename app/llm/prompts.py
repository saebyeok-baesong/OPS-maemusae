"""프롬프트. 넣는 입력마다 길이 상한을 둬서 토큰 비용을 통제한다. 입력은 타입이 정해진 장애이고 출력은 문자열이다."""
from app.incident import Incident


def clip(text: str | None, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head] + "\n...[생략]...\n" + text[-(limit - head):]


CLASSIFY_SYSTEM = """너는 매무새(Next.js web, FastAPI api) 장애 알림을 분류하는 SRE 보조 도구다.
알림과 함께 온 로그, 이벤트를 보고 장애 유형을 분류하고, 애플리케이션 코드 수정이 필요한지 판단한다.
- code_bug: 요청 처리 중 예외, 로직 오류. 애플리케이션 코드를 고쳐야 한다.
- config: 빠지거나 형식이 틀린 환경변수, Secret, 설정값. 기동 중 설정을 읽다 난 예외도 여기다.
- infra: 노드, 메모리나 디스크 같은 자원 부족(OOMKilled 포함), 네트워크, RDS나 외부 연동의 지연과 타임아웃.
- dependency: 라이브러리, 패키지 버전 문제.
- data: 잘못 들어간 데이터.
근거가 부족할 때만 unknown을 쓰고, 확신이 낮으면 confidence를 낮게 준다.
keywords에는 런북 검색에 쓸 단어를 3~6개 넣는다."""


def evidence_text(inc: Incident) -> str:
    ev = inc.evidence
    parts = []
    if ev.stacktrace:
        parts.append(f"[스택트레이스 마지막 부분{f' request_id={ev.request_id}' if ev.request_id else ''}]\n{ev.stacktrace[-1500:]}")
    if ev.pod_events:
        parts.append("[Pod 이벤트]\n" + "\n".join(ev.pod_events[-10:]))
    if ev.pod_logs:
        parts.append(f"[Pod 마지막 로그]\n{ev.pod_logs[-2000:]}")
    return "\n\n".join(parts) or "(근거 로그 없음)"


def classify_user(inc: Incident) -> str:
    return f"[앱] {inc.app}\n[알림]\n{clip(inc.message, 1500)}\n\n{evidence_text(inc)}"


ROOT_CAUSE_SYSTEM = """너는 매무새의 시니어 백엔드 엔지니어다. 장애 정보, 근거 로그, 사내 런북, 스택트레이스 위치의 코드를 보고 근본 원인을 추론한다.
코드가 없으면 추측이면 추측이라고 쓴다. evidence에는 근거가 된 로그 줄, 코드 위치, 문서를 적는다.
suspected_files에는 고칠 파일을 제공된 코드의 ### 경로 그대로 적는다. 코드가 없으면 비운다.
장애 직전에 배포가 있었으면 그 배포를 원인 후보로 검토하고, 그렇게 판단했으면 evidence에 이미지와 배포 시각을 적는다.
next_steps에는 담당 개발자가 바로 할 일을 적는다."""


def deploy_text(inc: Incident) -> str:
    d = inc.deploy
    if not d:
        return "(배포 정보 없음)"
    minutes = (inc.occurred_at - d.deployed_at).total_seconds() / 60
    when = f"장애 {minutes:.0f}분 전" if minutes >= 0 else "장애 이후 배포"
    line = f"{d.app} 현재 이미지 {d.image}, 배포 {d.deployed_at.isoformat()} ({when}), INFRA 커밋 {d.commit[:7]}"
    if d.previous:
        line += f"\n직전 이미지 {d.previous}"
    return line


def root_cause_user(inc: Incident, classification: dict, docs: str, code: str = "") -> str:
    return f"""[장애]
앱: {inc.app} (namespace {inc.namespace})
알림: {clip(inc.message, 1500)}

{evidence_text(inc)}

[분류] {classification['category']} - {classification['summary']}

[배포 이력]
{deploy_text(inc)}

[관련 런북]
{docs or '(검색 결과 없음)'}

[스택트레이스 위치의 코드]
{code or '(코드 없음)'}"""


FIX_SYSTEM = """너는 장애를 고치는 수정안을 작성한다. 사람이 검토하고 SERVICE 레포에 PR로 올린다. 규칙:
1. patch는 제공된 코드의 ### 경로 기준 unified diff다. 헤더는 '--- a/경로', '+++ b/경로'를 쓴다.
2. 원인을 고치는 최소 변경만 한다. 리팩터링, 포매팅 변경, 무관한 파일 수정은 하지 않는다.
3. 기존 함수의 시그니처와 정상 경로의 동작은 바꾸지 않는다. 시그니처를 바꾸면 호출하는 곳이 모두 영향 범위에 들어간다.
4. 이전 시도가 거절됐다면 그 사유를 읽고 같은 실수를 반복하지 않는다.
5. 컨텍스트 줄은 제공된 코드와 정확히 같아야 한다. 코드 앞의 줄 번호 표시('  12| ')는 patch에 넣지 않는다.
risk에는 이 변경이 정상 경로를 깨뜨릴 가능성을 low, medium, high로 적는다."""


def fix_user(inc: Incident, root_cause: dict, code: str, history: list[dict]) -> str:
    prev = "".join(f"\n--- 이전 시도 {i} (거절) ---\n[패치]\n{clip(h['patch'], 1500)}\n[사유] {h['reason']}\n"
                   for i, h in enumerate(history, 1))
    steps = "\n".join(f"- {s}" for s in root_cause.get("next_steps", []))
    return f"""[장애] {inc.app}: {clip(inc.message, 800)}

[근본 원인] {root_cause['root_cause']}
[의심 위치] {root_cause.get('suspected_area', '')}
[할 일]
{steps}

[수정 대상 코드]
{code}
{prev}"""


SECURITY_SYSTEM = """너는 보안팀의 일일 보고를 쓰는 도구다. 주어진 집계와 새 결과 목록만 근거로 쓴다.
summary는 세 줄 안으로, top_actions는 먼저 처리할 항목을 심각도 순으로 최대 다섯 개 적는다. 비밀값은 받지도 쓰지도 않는다."""


def security_user(counts: dict, new_findings: list[dict]) -> str:
    lines = [f"- [{f['severity']}] {f['source']} {f['target']} {f['rule_id']} {f['title'][:120]}" for f in new_findings[:40]]
    return f"[집계(도구 x 심각도)]\n{counts}\n\n[오늘 처음 나온 결과]\n" + ("\n".join(lines) or "(없음)")
