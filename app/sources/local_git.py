"""로컬 데모용 INFRA 레포. GitHub 대신 bare git 저장소 하나로 같은 인터페이스(InfraRepo)를 낸다.

PR은 브랜치 하나와 `ops-pulls.json`의 기록 하나다. merge는 GitHub squash처럼 base 위에 커밋 하나를 올린다.
브랜치와 main은 compare-and-swap(update-ref <새> <옛>)으로만 옮겨서, 제안 뒤 누가 밀어 넣었으면 merge하지 않는다.
INFRA_REPO_BACKEND=local-git일 때만 쓴다(demo/README.md).
"""
import fcntl
import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.deploys import ImagesFileError, Revision, parse_images, set_image
from app.sources.github import PullFile, PullRequest

STATUS = {"M": "modified", "A": "added", "D": "removed", "R": "renamed", "C": "copied", "T": "changed"}
BOT = ["-c", "user.name=ops-rollback-bot", "-c", "user.email=ops-rollback-bot@localhost"]


class GitError(RuntimeError):
    pass


class LocalGitInfraRepo:
    def __init__(self, git_dir: str, branch: str, images_path: str, pr_url_base: str):
        self.git_dir, self.branch, self.path = Path(git_dir), branch, images_path
        self.pr_url_base = pr_url_base.rstrip("/")
        self.pulls_file = self.git_dir / "ops-pulls.json"

    def _git(self, *args: str, input: str | None = None, env: dict | None = None, ok: tuple[int, ...] = (0,)) -> str:
        # 볼륨 담당 uid가 컨테이너마다 달라서 safe.directory를 연다. 데모 전용 저장소라 괜찮다
        cmd = ["git", "-c", "safe.directory=*", *BOT, f"--git-dir={self.git_dir}", *args]
        proc = subprocess.run(cmd, input=input, capture_output=True, text=True, env={**os.environ, **(env or {})})
        if proc.returncode not in ok:
            raise GitError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def _rev(self, ref: str) -> str | None:
        out = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{ref}", ok=(0, 1)).strip()
        return out or None

    def _file(self, ref: str) -> str:
        return self._git("show", f"{ref}:{self.path}")

    def history(self, depth: int) -> Iterator[Revision]:
        out = self._git("log", f"-n{depth}", "--format=%H%x1f%cI%x1f%s", self.branch, "--", self.path)
        for line in out.splitlines():
            sha, date, message = line.split("\x1f", 2)
            try:
                images = parse_images(self._file(sha))
            except ImagesFileError:
                continue
            yield Revision(sha, datetime.fromisoformat(date), message, images)

    def current_images(self) -> dict[str, str]:
        return parse_images(self._file(self.branch))

    def _commit_file(self, branch: str, text: str, message: str) -> str:
        """브랜치 위에 images.yaml만 바꾼 커밋 하나. 작업 트리 없이 임시 index로 만든다."""
        parent = self._rev(branch)
        blob = self._git("hash-object", "-w", "--stdin", input=text).strip()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
            self._git("read-tree", parent, env=env)
            self._git("update-index", "--cacheinfo", f"100644,{blob},{self.path}", env=env)
            tree = self._git("write-tree", env=env).strip()
        commit = self._git("commit-tree", tree, "-p", parent, "-m", message).strip()
        self._git("update-ref", f"refs/heads/{branch}", commit, parent)
        return commit

    @contextmanager
    def _pulls(self) -> Iterator[list[dict]]:
        """api, worker, approvals가 다른 컨테이너에서 같은 파일을 쓴다. 파일 잠금 하나로 줄 세운다."""
        with open(self.git_dir / "ops-pulls.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pulls = json.loads(self.pulls_file.read_text()) if self.pulls_file.exists() else []
            before = json.dumps(pulls)
            yield pulls
            if json.dumps(pulls) != before:
                self.pulls_file.write_text(json.dumps(pulls, ensure_ascii=False, indent=1))

    def _pr(self, p: dict) -> PullRequest:
        return PullRequest(p["number"], f"{self.pr_url_base}/{p['number']}", self._rev(p["head"]) or "",
                           p["state"], p["merged"], p["base"])

    def open_rollback_pr(self, branch: str, app: str, image: str, title: str, body: str) -> PullRequest:
        """같은 branch로 다시 불러도 PR은 하나다. 중간에 죽었으면 남은 단계만 한다."""
        with self._pulls() as pulls:
            if existing := next((p for p in pulls if p["head"] == branch), None):
                return self._pr(existing)
            if self._rev(branch) is None:
                self._git("update-ref", f"refs/heads/{branch}", self._rev(self.branch), "")
            text = self._file(branch)
            if parse_images(text).get(app) != image:
                self._commit_file(branch, set_image(text, app, image), title)
            pulls.append({"number": len(pulls) + 1, "head": branch, "base": self.branch, "title": title, "body": body,
                          "state": "open", "merged": False})
            return self._pr(pulls[-1])

    def _find(self, pulls: list[dict], number: int) -> dict:
        return next(p for p in pulls if p["number"] == number)

    def pull(self, number: int) -> PullRequest:
        with self._pulls() as pulls:
            return self._pr(self._find(pulls, number))

    def pull_files(self, number: int) -> list[PullFile]:
        with self._pulls() as pulls:
            p = self._find(pulls, number)
        base = self._git("merge-base", p["base"], p["head"]).strip()
        files = []
        for line in self._git("diff", "--name-status", base, p["head"]).splitlines():
            status, *names = line.split("\t")
            name = names[-1]
            patch = self._git("diff", base, p["head"], "--", *names)
            hunks = patch[patch.find("\n@@") + 1:] if "\n@@" in patch else None
            files.append(PullFile(name, STATUS.get(status[0], status), hunks))
        return files

    def merge(self, number: int, head_sha: str, title: str) -> tuple[bool, str]:
        """GitHub squash merge와 같다. head_sha가 다르거나 base와 충돌하면 merge하지 않는다."""
        with self._pulls() as pulls:
            p = self._find(pulls, number)
            if p["merged"]:
                return False, f"PR #{number} is already merged"
            if self._rev(p["head"]) != head_sha:
                return False, "Head branch was modified"
            base = self._rev(p["base"])
            try:
                tree = self._git("merge-tree", "--write-tree", base, head_sha).splitlines()[0].strip()
            except GitError:
                return False, "Merge conflict"
            commit = self._git("commit-tree", tree, "-p", base, "-m", title).strip()
            self._git("update-ref", f"refs/heads/{p['base']}", commit, base)
            p.update(state="closed", merged=True)
            return True, commit


def make_local_infra_repo(settings) -> LocalGitInfraRepo:
    s = settings
    return LocalGitInfraRepo(s.infra_local_git_dir, s.infra_branch, s.images_path, s.infra_local_pr_url)
