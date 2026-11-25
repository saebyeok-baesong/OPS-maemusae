from datetime import datetime, timedelta, timezone

import pytest

from app import rollback
from app.deploys import parse_images
from app.incident import Deploy, Incident
from app.rollback import RollbackPlan, Status, approve, propose
from tests.conftest import DEPLOYED_AT, INFRA_ROLE
from tests.fakes import image

WINDOW = timedelta(minutes=30)


def incident(alertname="MaemusaeUnhandledException", app="api", minutes_after=4, previous=True, by_rollback=False) -> Incident:
    deploy = Deploy(app=app, image=image(app, "a2"), previous=image(app, "a1") if previous else None,
                    deployed_at=DEPLOYED_AT, commit="c2", by_rollback=by_rollback)
    return Incident(alertname=alertname, alert_fingerprint="fp", app=app, namespace="maemusae", severity="critical",
                    message="[x] y", occurred_at=DEPLOYED_AT + timedelta(minutes=minutes_after), deploy=deploy)


PLAN = RollbackPlan(app="api", from_image=image("api", "a2"), to_image=image("api", "a1"), deployed_at=DEPLOYED_AT)


@pytest.mark.parametrize("inc, expected", [
    (incident(), PLAN),
    (incident("KubePodCrashLooping"), PLAN),
    (incident(minutes_after=45), None),
    (incident(minutes_after=-1), None),
    (incident(previous=False), None),
    (incident("KubePodOOMKilled"), None),
    (incident(app="worker"), None),
    (incident(by_rollback=True), None),
])
def test_rollback_is_proposed_only_right_after_a_deploy(inc, expected):
    assert propose(inc, ["web", "api"], WINDOW) == expected


def test_rollback_pr_changes_only_the_app_line_and_is_idempotent(container, github):
    before = parse_images(github.main_images_text())
    pr = rollback.open_pr(container.infra, "inc1", incident(), PLAN)
    again = rollback.open_pr(container.infra, "inc1", incident(), PLAN)

    assert pr == again and len(github.pulls) == 1
    assert pr.url.endswith("/INFRA-watch-nyang/pull/1")
    head = github.branches["ops/rollback-inc1-api"][0]
    assert parse_images(head.text) == {**before, "api": image("api", "a1")}
    assert github.pulls[0]["title"] == f"ops: api 롤백 {rollback.short(PLAN.from_image)} -> {rollback.short(PLAN.to_image)}"
    assert parse_images(github.main_images_text()) == before, "승인 전에는 main을 바꾸지 않는다"
    assert github.tokens_issued == 1, "설치 토큰은 만료 전까지 재사용한다"


def with_proposal(c, github, incident_id="inc1"):
    inc = incident()
    c.store.create(incident_id, "fp", inc)
    pr = rollback.open_pr(c.infra, incident_id, inc, PLAN)
    c.store.set_status(incident_id, "analyzed", rollback_pr=pr.model_dump(mode="json"))
    return pr


def test_approval_merges_once_and_records_the_action(container, github):
    with_proposal(container, github)
    first = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    second = approve(container.store, container.infra, "inc1", "U-BOB", [INFRA_ROLE], [INFRA_ROLE])

    assert first.status == Status.MERGED and second.status == Status.ALREADY
    assert parse_images(github.main_images_text())["api"] == image("api", "a1")
    assert "[ops-rollback]" in github.branches["main"][0].message
    [action] = container.store.actions("inc1")
    assert (action["action"], action["target"], action["approver"], action["status"]) == ("rollback", "api", "U-ALICE", "merged")
    assert [m for m, p in github.calls if p.endswith("/merge")] == ["PUT"], "merge는 한 번"


def test_after_the_bot_rollback_the_next_incident_is_not_rolled_back_again(container, github):
    with_proposal(container, github)
    approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    from app.deploys import deploy_from_history

    d = deploy_from_history("api", container.infra.history(20))
    assert d.by_rollback and d.previous == image("api", "a2")
    later = incident().model_copy(update={"deploy": d, "occurred_at": datetime.now(timezone.utc)})
    assert propose(later, ["api"], WINDOW) is None, "방금 되돌린 나쁜 이미지로 다시 롤백하지 않는다"


def test_non_infra_member_cannot_approve(container, github):
    with_proposal(container, github)
    assert approve(container.store, container.infra, "inc1", "U-DEV", ["R-FULLSTACK"], [INFRA_ROLE]).status == Status.NOT_ALLOWED
    assert approve(container.store, container.infra, "inc1", "U-ANY", [INFRA_ROLE], []).status == Status.NOT_ALLOWED, \
        "승인 역할을 설정하지 않으면 아무도 승인하지 못한다"
    assert container.store.actions("inc1") == []


def test_approval_refuses_when_the_image_moved_after_the_proposal(container, github):
    with_proposal(container, github)
    from tests.fakes import images_yaml

    github.commit("main", images_yaml(web=image("web", "w1"), api=image("api", "a3")), "deploy(api): a3", datetime.now(timezone.utc))
    result = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.STALE and "바뀌었다" in result.detail
    assert parse_images(github.main_images_text())["api"] == image("api", "a3")
    assert container.store.actions("inc1")[0]["status"] == "stale"


def test_approval_refuses_when_someone_pushed_to_the_pr(container, github):
    pr = with_proposal(container, github)
    head = github.branches["ops/rollback-inc1-api"][0]
    github.commit("ops/rollback-inc1-api", head.text + "# 손으로 고침\n", "fix", datetime.now(timezone.utc))
    result = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.STALE and f"#{pr.number}" in result.detail


def test_incident_without_proposal(container):
    container.store.create("inc2", "fp", incident())
    assert approve(container.store, container.infra, "inc2", "U", [INFRA_ROLE], [INFRA_ROLE]).status == Status.NO_PROPOSAL
    assert approve(container.store, container.infra, "nope", "U", [INFRA_ROLE], [INFRA_ROLE]).status == Status.UNKNOWN


def test_any_of_the_configured_team_roles_can_approve(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DISCORD_APPROVER_ROLE_IDS", "R-FULLSTACK, R-INFRA,R-SECURITY")
    roles = Settings(_env_file=None).discord_approver_role_ids
    assert roles == ["R-FULLSTACK", "R-INFRA", "R-SECURITY"]
    monkeypatch.setenv("DISCORD_APPROVER_ROLE_IDS", "")
    assert Settings(_env_file=None).discord_approver_role_ids == []


@pytest.mark.parametrize("member_roles", [["R-FULLSTACK"], ["R-OTHER", "R-SECURITY"]])
def test_member_with_any_approver_role_merges(container, github, member_roles):
    with_proposal(container, github)
    result = approve(container.store, container.infra, "inc1", "U", member_roles, ["R-FULLSTACK", "R-INFRA", "R-SECURITY"])
    assert result.status == Status.MERGED


def leftover_branch(github, text: str) -> None:
    """이전 시도가 남긴 롤백 브랜치. open_pr은 이 브랜치를 그대로 PR로 연다."""
    github.branches["ops/rollback-inc1-api"] = [github.branches["main"][0]]
    github.commit("ops/rollback-inc1-api", text, "rollback", datetime.now(timezone.utc))


def test_pr_touching_another_file_is_not_merged(container, github):
    with_proposal(container, github)
    github.extra_files["ops/rollback-inc1-api"] = [("gitops/security/networkpolicy.yaml", "modified")]
    result = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.REJECTED and "gitops/security/networkpolicy.yaml" in result.detail
    assert parse_images(github.main_images_text())["api"] == image("api", "a2")
    assert not [p for _, p in github.calls if p.endswith("/merge")]
    assert container.store.actions("inc1")[0]["status"] == "rejected"


def test_pr_changing_two_lines_is_not_merged(container, github):
    from tests.fakes import images_yaml

    leftover_branch(github, images_yaml(web=image("web", "evil"), api=image("api", "a1")))
    with_proposal(container, github)
    result = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.REJECTED and "한 줄이 아니다" in result.detail
    assert parse_images(github.main_images_text())["web"] == image("web", "w1")


def test_diff_must_be_exactly_the_planned_line():
    from app.sources.github import PullFile

    def patch(old: str, new: str) -> list[PullFile]:
        return [PullFile("gitops/apps/images.yaml", "modified", f"@@ -3,3 +3,3 @@\n \n-{old}\n+{new}\n ")]

    planned = patch(f"api: {PLAN.from_image}", f"api: {PLAN.to_image}")
    assert rollback.diff_problem(PLAN, "main", planned, "main", "gitops/apps/images.yaml") is None
    other_image = patch(f"api: {PLAN.from_image}", f"api: {image('api', 'other')}")
    assert "api" in rollback.diff_problem(PLAN, "main", other_image, "main", "gitops/apps/images.yaml")
    other_app = patch(f"web: {image('web', 'w1')}", f"web: {image('web', 'w0')}")
    assert rollback.diff_problem(PLAN, "main", other_app, "main", "gitops/apps/images.yaml")
    renamed = [PullFile("gitops/apps/images.yaml", "renamed", planned[0].patch)]
    assert rollback.diff_problem(PLAN, "main", renamed, "main", "gitops/apps/images.yaml")


def test_pr_not_targeting_main_is_not_merged(container, github):
    with_proposal(container, github)
    github.pulls[0]["base"] = "release"
    github.branches["release"] = list(github.branches["main"])
    result = approve(container.store, container.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.REJECTED and "release" in result.detail
