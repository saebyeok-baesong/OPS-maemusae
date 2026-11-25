"""로컬 데모의 INFRA 레포(bare git 저장소)가 GitHub InfraRepo와 같은 롤백 흐름을 지나가는지 본다."""
import os
import subprocess
from datetime import timedelta

import pytest

from app import rollback
from app.deploys import deploy_from_history, parse_images
from app.rollback import Status, approve, propose
from app.sources.local_git import LocalGitInfraRepo
from tests.conftest import DEPLOYED_AT, INFRA_ROLE
from tests.fakes import image, images_yaml
from tests.test_rollback import PLAN, WINDOW, incident

PATH = "gitops/apps/images.yaml"


def git(repo, *args, date=None, input=None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env |= {"GIT_AUTHOR_DATE": date.isoformat(), "GIT_COMMITTER_DATE": date.isoformat()}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, input=input, text=True, check=True,
                          capture_output=True).stdout


def push_images(work, text: str, message: str, date=None, branch="main", extra: dict | None = None) -> None:
    """branch가 원격에 없으면 origin/main에서 시작한다. 빈 저장소의 첫 커밋은 그대로 main에 올린다."""
    git(work, "fetch", "-q", "origin")
    start = next((f"origin/{b}" for b in (branch, "main") if git(work, "ls-remote", "origin", b).strip()), None)
    if start:
        git(work, "checkout", "-q", "-B", branch, start)
    (work / PATH).parent.mkdir(parents=True, exist_ok=True)
    (work / PATH).write_text(text)
    for name, content in (extra or {}).items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message, date=date)
    git(work, "push", "-q", "origin", f"HEAD:{branch}")


@pytest.fixture
def repo(tmp_path):
    """main 이력: 어제 첫 배포 -> 픽스처 장애 4분 전에 api 새 이미지. conftest의 github 픽스처와 같은 모양."""
    bare, work = tmp_path / "infra.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    push_images(work, images_yaml(web=image("web", "w1"), api=image("api", "a1")), "chore: 첫 배포",
                DEPLOYED_AT - timedelta(days=1))
    push_images(work, images_yaml(web=image("web", "w1"), api=image("api", "a2")), "deploy(api): a2", DEPLOYED_AT)
    return LocalGitInfraRepo(str(bare), "main", PATH, "http://localhost/local-gitops/pulls"), work


@pytest.fixture
def local(container, repo):
    container.__dict__["infra"] = repo[0]
    return container


def main_images(infra) -> dict[str, str]:
    return infra.current_images()


def with_proposal(c, incident_id="inc1"):
    inc = incident()
    c.store.create(incident_id, "fp", inc)
    pr = rollback.open_pr(c.infra, incident_id, inc, PLAN)
    c.store.set_status(incident_id, "analyzed", rollback_pr=pr.model_dump(mode="json"))
    return pr


def test_history_gives_the_deploy_and_the_previous_image(repo):
    infra, _ = repo
    d = deploy_from_history("api", infra.history(20))
    assert (d.image, d.previous, d.deployed_at, d.by_rollback) == (image("api", "a2"), image("api", "a1"), DEPLOYED_AT, False)
    assert propose(incident().model_copy(update={"deploy": d}), ["api"], WINDOW) == PLAN


def test_rollback_pr_is_a_branch_and_opening_it_again_is_a_no_op(local):
    pr = rollback.open_pr(local.infra, "inc1", incident(), PLAN)
    again = rollback.open_pr(local.infra, "inc1", incident(), PLAN)

    assert pr == again and pr.number == 1 and pr.url == "http://localhost/local-gitops/pulls/1"
    assert main_images(local.infra)["api"] == image("api", "a2"), "승인 전에는 main을 바꾸지 않는다"
    assert parse_images(local.infra._file("ops/rollback-inc1-api"))["api"] == image("api", "a1")
    [changed] = local.infra.pull_files(pr.number)
    assert rollback.diff_problem(PLAN, "main", [changed], "main", PATH) is None, "GitHub와 같은 diff 확인을 지나간다"


def test_approval_merges_once_and_the_next_incident_is_not_rolled_back_again(local):
    with_proposal(local)
    first = approve(local.store, local.infra, "inc1", "U-ALICE", [INFRA_ROLE], [INFRA_ROLE])
    second = approve(local.store, local.infra, "inc1", "U-BOB", [INFRA_ROLE], [INFRA_ROLE])

    assert first.status == Status.MERGED and second.status == Status.ALREADY
    assert main_images(local.infra) == {"web": image("web", "w1"), "api": image("api", "a1")}
    d = deploy_from_history("api", local.infra.history(20))
    assert d.by_rollback and d.previous == image("api", "a2")
    assert local.infra.pull(1).merged


def test_merge_keeps_other_apps_changed_on_main_after_the_proposal(local, repo):
    with_proposal(local)
    push_images(repo[1], images_yaml(web=image("web", "w2"), api=image("api", "a2")), "deploy(web): w2")

    assert approve(local.store, local.infra, "inc1", "U", [INFRA_ROLE], [INFRA_ROLE]).status == Status.MERGED
    assert main_images(local.infra) == {"web": image("web", "w2"), "api": image("api", "a1")}


def test_approval_refuses_when_the_image_moved_after_the_proposal(local, repo):
    with_proposal(local)
    push_images(repo[1], images_yaml(web=image("web", "w1"), api=image("api", "a3")), "deploy(api): a3")

    result = approve(local.store, local.infra, "inc1", "U", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.STALE and "바뀌었다" in result.detail
    assert main_images(local.infra)["api"] == image("api", "a3")


def test_approval_refuses_when_someone_pushed_to_the_pr_branch(local, repo):
    with_proposal(local)
    push_images(repo[1], local.infra._file("ops/rollback-inc1-api") + "# 손으로 고침\n", "fix", branch="ops/rollback-inc1-api")

    result = approve(local.store, local.infra, "inc1", "U", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.STALE and "#1" in result.detail
    assert main_images(local.infra)["api"] == image("api", "a2")


def test_pr_touching_another_file_is_rejected(local, repo):
    push_images(repo[1], images_yaml(web=image("web", "w1"), api=image("api", "a1")), "rollback",
                branch="ops/rollback-inc1-api", extra={"gitops/security/networkpolicy.yaml": "kind: NetworkPolicy\n"})
    with_proposal(local)

    result = approve(local.store, local.infra, "inc1", "U", [INFRA_ROLE], [INFRA_ROLE])
    assert result.status == Status.REJECTED and "gitops/security/networkpolicy.yaml" in result.detail
    assert main_images(local.infra)["api"] == image("api", "a2")


def test_cli_approval_works_without_discord_and_is_recorded_with_the_operator(local):
    from app.approvals import approve_from_cli

    with_proposal(local)
    local.store.set_card("inc1", "C-INC", "m1")
    result = approve_from_cli(local, "inc1", "infra-lead")

    assert result.status == Status.MERGED and main_images(local.infra)["api"] == image("api", "a1")
    assert local.store.actions("inc1")[0]["approver"] == "cli:infra-lead"
    [(channel, msg, reply_to)] = local.discord.posts
    assert (channel, reply_to) == ("C-INC", "m1") and "`cli:infra-lead`" in msg["content"]
    assert approve_from_cli(local, "inc1", "someone").status == Status.ALREADY


def test_cli_approval_is_off_unless_enabled(local, monkeypatch):
    from app import approvals

    with_proposal(local)
    monkeypatch.setattr(approvals, "Container", lambda: local)
    monkeypatch.setattr("sys.argv", ["app.approvals", "approve", "inc1", "--by", "someone"])
    with pytest.raises(SystemExit, match="CLI_APPROVAL_ENABLED"):
        approvals.main()
    assert local.store.actions("inc1") == []

    local.settings.cli_approval_enabled = True
    with pytest.raises(SystemExit) as done:
        approvals.main()
    assert done.value.code == 0 and local.store.actions("inc1")[0]["status"] == "merged"
