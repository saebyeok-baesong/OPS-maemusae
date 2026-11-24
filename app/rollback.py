"""승인 후 조치. 허용된 조치는 하나뿐이다: INFRA `images.yaml`에서 앱 이미지를 직전 digest로 되돌리는 PR을 열고,
인프라팀이 Discord에서 승인하면 그 PR을 merge한다. Argo CD가 sync하고 self-heal은 켜 둔 채다.

조치 대상과 이미지는 알림 라벨과 INFRA git 이력에서만 정한다. LLM 출력은 쓰지 않는다.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from app.alerts import ALERT_KINDS
from app.deploys import ROLLBACK_MARK
from app.incident import Incident

ACTION = "rollback"


class RollbackPlan(BaseModel):
    app: str
    from_image: str
    to_image: str
    deployed_at: datetime


class RollbackPR(BaseModel):
    plan: RollbackPlan
    number: int
    url: str
    head_sha: str


def short(image: str) -> str:
    return image.rsplit("@sha256:", 1)[-1][:12]


def propose(inc: Incident, apps: list[str], window: timedelta) -> RollbackPlan | None:
    """배포 직후에 시작된 장애이고 되돌릴 직전 이미지가 있을 때만. 분석 결과와 상관없이 빠른 완화로 제안한다."""
    kind, d = ALERT_KINDS.get(inc.alertname), inc.deploy
    if not (kind and kind.rollback and d and d.previous) or d.by_rollback:
        return None
    if inc.app not in apps or d.app != inc.app:
        return None
    if not timedelta(0) <= inc.occurred_at - d.deployed_at <= window:
        return None
    return RollbackPlan(app=d.app, from_image=d.image, to_image=d.previous, deployed_at=d.deployed_at)


def branch_name(incident_id: str, app: str) -> str:
    return f"ops/rollback-{incident_id}-{app}"


def pr_title(plan: RollbackPlan) -> str:
    return f"ops: {plan.app} 롤백 {short(plan.from_image)} -> {short(plan.to_image)}"


def pr_body(incident_id: str, inc: Incident, plan: RollbackPlan) -> str:
    return (f"장애 `{incident_id}` ({inc.alertname})가 배포 직후에 시작되어 직전 이미지로 되돌립니다.\n\n"
            f"- 앱: `{plan.app}`\n- 지금: `{plan.from_image}` (배포 {plan.deployed_at.isoformat()})\n"
            f"- 되돌릴 이미지: `{plan.to_image}`\n\n"
            "Discord 장애 카드에서 인프라팀이 승인하면 장애 대응 봇이 이 PR을 merge합니다. 직접 merge하거나 닫아도 됩니다.")


def merge_title(plan: RollbackPlan, number: int) -> str:
    return f"ops: {plan.app} 롤백 {ROLLBACK_MARK} (#{number})"


def open_pr(infra, incident_id: str, inc: Incident, plan: RollbackPlan) -> RollbackPR:
    pr = infra.open_rollback_pr(branch_name(incident_id, plan.app), plan.app, plan.to_image, pr_title(plan),
                                pr_body(incident_id, inc, plan))
    return RollbackPR(plan=plan, number=pr.number, url=pr.url, head_sha=pr.head_sha)


class Status(StrEnum):
    MERGED = "merged"
    FAILED = "failed"
    STALE = "stale"               # 제안 뒤 배포나 PR이 바뀌었다
    ALREADY = "already"
    NOT_ALLOWED = "not_allowed"
    NO_PROPOSAL = "no_proposal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Approval:
    status: Status
    detail: str = ""
    pr: RollbackPR | None = None


def approve(store, infra, incident_id: str, approver: str, approver_roles: list[str], allowed_roles: list[str]) -> Approval:
    """버튼 값은 장애 ID뿐이다. 무엇을 merge할지는 장애 기록에서 다시 읽고, merge 직전에 배포가 그대로인지 다시 본다."""
    if not set(approver_roles) & set(allowed_roles):
        return Approval(Status.NOT_ALLOWED)
    record = store.get(incident_id)
    if record is None:
        return Approval(Status.UNKNOWN)
    raw = record.result.get("rollback_pr")
    if not raw or infra is None:
        return Approval(Status.NO_PROPOSAL)
    pr = RollbackPR.model_validate(raw)
    if not store.start_action(incident_id, ACTION, pr.plan.app, approver):
        return Approval(Status.ALREADY, pr=pr)

    status, detail = _merge(infra, pr)
    store.finish_action(incident_id, ACTION, status, detail[-4000:])
    return Approval(status, detail, pr)


def _merge(infra, pr: RollbackPR) -> tuple[Status, str]:
    try:
        now = infra.current_images().get(pr.plan.app)
        if now != pr.plan.from_image:
            return Status.STALE, f"제안 뒤 {pr.plan.app} 이미지가 바뀌었다(제안 {short(pr.plan.from_image)}, 지금 {short(now or '-')}). merge하지 않았다"
        current = infra.pull(pr.number)
        if current.merged:
            return Status.MERGED, f"PR #{pr.number}은 이미 merge되어 있다"
        if current.state != "open":
            return Status.STALE, f"PR #{pr.number}이 닫혀 있다. merge하지 않았다"
        if current.head_sha != pr.head_sha:
            return Status.STALE, f"제안 뒤 PR #{pr.number}에 커밋이 더해졌다. merge하지 않았다"
        ok, detail = infra.merge(pr.number, pr.head_sha, merge_title(pr.plan, pr.number))
    except Exception as e:  # noqa: BLE001 - 실패도 조치 기록과 회신으로 남긴다
        return Status.FAILED, repr(e)
    if ok:
        return Status.MERGED, f"PR #{pr.number} merge {detail[:7]}. Argo CD sync 뒤 {short(pr.plan.to_image)}로 돌아간다"
    return Status.FAILED, detail
