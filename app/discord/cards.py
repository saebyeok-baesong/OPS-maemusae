"""Discord 메시지 본문을 만든다(순수 함수). 장애 하나에 메시지 하나, 보안 일일 보고 하나에 메시지 하나.

Discord 한도: embed 제목 256, 설명 4096, 필드 값 1024, 필드 25개, embed 합계 6000자, 버튼 라벨 80, custom_id 100.
"""
from dataclasses import dataclass

from app.graph.nodes import impact_summary
from app.incident import Incident
from app.rollback import RollbackPR, short

TITLE_MAX, DESC_MAX, FIELD_MAX, LABEL_MAX = 256, 4096, 1024, 80
# 메시지는 한눈에 읽는 요약이다. 전문은 장애 조회 API의 report에 남는다
BODY_MAX = 400
APPROVE_PREFIX = "rollback:"


@dataclass(frozen=True)
class Look:
    emoji: str
    title: str
    color: int


LOOKS = {
    "runbook": Look("📘", "코드 수정 대상 아님 · 런북 안내", 0x3498DB),
    "analyzed": Look("🔎", "코드 수정 필요 · 원인 분석", 0xE67E22),
    "fix_proposed": Look("🛠️", "코드 수정 필요 · 원인 분석과 수정안", 0xE67E22),
    "budget_exceeded": Look("💸", "LLM 비용 상한 도달 · 담당자 확인", 0x95A5A6),
    "failed": Look("❌", "자동 분석 실패 · 담당자 확인", 0xE74C3C),
}
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _field(name: str, value: str, inline: bool = False) -> dict:
    return {"name": name, "value": clip(value or "-", FIELD_MAX), "inline": inline}


def _evidence(inc: Incident) -> str:
    ev, lines = inc.evidence, []
    if ev.stacktrace:
        last = [line for line in ev.stacktrace.strip().splitlines() if line.strip()][-1]
        lines.append(f"`{clip(last, 200)}`" + (f" · request_id `{ev.request_id}`" if ev.request_id else ""))
    lines += [f"이벤트: {clip(e, 150)}" for e in ev.pod_events[-3:]]
    if ev.pod_logs and not ev.stacktrace:
        last = [line for line in ev.pod_logs.strip().splitlines() if line.strip()]
        if last:
            lines.append(f"마지막 로그: `{clip(last[-1], 200)}`")
    if d := inc.deploy:
        minutes = (inc.occurred_at - d.deployed_at).total_seconds() / 60
        lines.append(f"배포: `{short(d.image)}` ({minutes:.0f}분 전)" + (f", 직전 `{short(d.previous)}`" if d.previous else ""))
    return "\n".join(lines)


def _runbooks(docs: list[dict], base_url: str) -> str:
    ids = list(dict.fromkeys(d["doc_id"] for d in docs))[:5]
    return "\n".join(f"[{i}]({base_url.rstrip('/')}/{i})" for i in ids)


def incident_card(incident_id: str, state: dict, rollback: RollbackPR | None, runbook_base_url: str,
                  rollback_note: str | None = None) -> dict:
    inc = Incident.model_validate(state["incident"])
    outcome = state.get("outcome", "failed")
    look = LOOKS.get(outcome, Look("❓", outcome, 0x95A5A6))
    cls = state.get("classification") or {}
    cost = state.get("cost") or {}

    fields = [
        _field("앱", f"`{inc.app}` ({inc.namespace})", inline=True),
        _field("분류", f"{cls['category']} ({cls['confidence']:.2f})" if cls else "-", inline=True),
        _field("LLM 비용", f"${cost.get('usd', 0):.4f}", inline=True),
    ]
    if cls:
        fields.append(_field("요약", clip(cls["summary"], BODY_MAX)))
    if rc := state.get("root_cause"):
        steps = "\n".join(f"- {s}" for s in rc.get("next_steps", [])[:3])
        fields.append(_field("원인", clip(rc["root_cause"], BODY_MAX) + (f"\n{steps}" if steps else "")))
    if (fix := state.get("fix")) and (impact := state.get("impact")):
        src = state["source"]
        files = "\n".join(f"`{src['dir']}/{f['path']}` +{f['added']} -{f['removed']}" for f in state["diff"]["files"])
        fields.append(_field(f"수정안 (SERVICE `{src['sha'][:7]}`, 위험도 {fix['risk']})",
                             clip(fix["explanation"], BODY_MAX) + f"\n{files}"))
        fields.append(_field("영향 범위", "\n".join(impact_summary(impact))))
        fields.append(_field("diff", f"```diff\n{clip(fix['patch'], FIELD_MAX - 20)}\n```"))
    elif state.get("fix"):
        fields.append(_field("수정안", f"{state.get('fix_attempts', 0)}번 만들었지만 diff 형식이 아니어서 버렸다"))
    if evidence := _evidence(inc):
        fields.append(_field("근거", evidence))
    if docs := state.get("context_docs"):
        fields.append(_field("런북", _runbooks(docs, runbook_base_url)))
    if rollback:
        p = rollback.plan
        fields.append(_field("롤백 제안", f"`{p.app}` `{short(p.from_image)}` -> `{short(p.to_image)}` · [INFRA PR #{rollback.number}]({rollback.url})"))
    elif rollback_note:
        fields.append(_field("롤백", rollback_note))
    if state.get("error"):
        fields.append(_field("중단 사유", clip(state["error"], BODY_MAX)))

    embed = {
        "title": clip(f"{look.emoji} {look.title}", TITLE_MAX),
        "description": clip(f"`{inc.message.splitlines()[0]}`", 600),
        "color": look.color,
        "fields": fields,
        "footer": {"text": f"incident {incident_id} · {inc.alertname}"},
        "timestamp": inc.occurred_at.isoformat(),
    }
    message = {"content": clip(f"{look.emoji} {inc.app} · {inc.message.splitlines()[0]}", 200), "embeds": [embed],
               "allowed_mentions": {"parse": []}}
    if rollback:
        message["components"] = [approve_row(incident_id, rollback)]
    return message


def approve_row(incident_id: str, rollback: RollbackPR) -> dict:
    """버튼 값은 장애 ID뿐이다. 무엇을 merge할지는 승인 봇이 장애 기록에서 다시 읽는다."""
    p = rollback.plan
    return {"type": 1, "components": [
        {"type": 2, "style": 4, "custom_id": f"{APPROVE_PREFIX}{incident_id}",
         "label": clip(f"롤백 승인: {p.app} -> {short(p.to_image)}", LABEL_MAX)},
        {"type": 2, "style": 5, "url": rollback.url, "label": f"INFRA PR #{rollback.number}"},
    ]}


def reply(text: str) -> dict:
    return {"content": clip(text, 1900), "allowed_mentions": {"parse": []}}


def security_card(report_date: str, counts: dict[str, dict[str, int]], new_count: int, summary: str,
                  top_actions: list[str]) -> dict:
    """counts: 도구 -> 심각도 -> 건수. 새 결과가 없으면 초록색으로 '이상 없음'이 보이게 한다."""
    worst = next((s for s in SEVERITY_ORDER if any(c.get(s) for c in counts.values())), None)
    color = {"CRITICAL": 0xE74C3C, "HIGH": 0xE67E22}.get(worst, 0x2ECC71)
    fields = [
        _field(source, " · ".join(f"{s} {c[s]}" for s in SEVERITY_ORDER if c.get(s)) or "0", inline=True)
        for source, c in sorted(counts.items())
    ]
    fields.append(_field("오늘 처음 나온 결과", f"{new_count}건"))
    fields.append(_field("요약", clip(summary, BODY_MAX)))
    if top_actions:
        fields.append(_field("먼저 할 일", "\n".join(f"- {a}" for a in top_actions[:5])))
    embed = {"title": f"🛡️ 보안 일일 보고 · {report_date}", "color": color, "fields": fields[:25]}
    return {"content": f"🛡️ 보안 일일 보고 {report_date}", "embeds": [embed], "allowed_mentions": {"parse": []}}
