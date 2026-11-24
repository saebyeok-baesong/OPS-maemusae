"""INFRA 레포를 GitHub App 자격으로 읽고 쓴다. 배포 이력 조회, 롤백 PR 열기, 승인된 PR merge.

App 권한은 INFRA 레포 하나에 contents, pull requests 쓰기만 준다. git clone 없이 REST API만 쓴다.
"""
import base64
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import httpx
import jwt

from app.deploys import ImagesFileError, Revision, parse_images, set_image

log = logging.getLogger(__name__)
API = "https://api.github.com"


class GitHubAppAuth:
    """설치 토큰은 1시간 유효하다. 만료 5분 전에 새로 받는다."""

    def __init__(self, app_id: str, installation_id: str, private_key: str, http: httpx.Client):
        self.app_id, self.installation_id, self.private_key, self.http = app_id, installation_id, private_key, http
        self._token, self._expires = "", 0.0

    def token(self) -> str:
        if time.time() < self._expires - 300:
            return self._token
        now = int(time.time())
        app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")
        resp = self.http.post(f"/app/installations/{self.installation_id}/access_tokens",
                              headers={"Authorization": f"Bearer {app_jwt}"})
        resp.raise_for_status()
        body = resp.json()
        self._token = body["token"]
        self._expires = datetime.fromisoformat(body["expires_at"]).timestamp()
        return self._token


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    head_sha: str
    state: str          # open | closed
    merged: bool


def _pr(body: dict) -> PullRequest:
    return PullRequest(body["number"], body["html_url"], body["head"]["sha"], body["state"], bool(body.get("merged")))


class InfraRepo:
    def __init__(self, repo: str, branch: str, images_path: str, auth: GitHubAppAuth, http: httpx.Client):
        self.repo, self.branch, self.path, self.auth, self.http = repo, branch, images_path, auth, http

    def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", **kwargs.pop("headers", {})}
        return self.http.request(method, f"/repos/{self.repo}{url}", headers=headers, **kwargs)

    def _file(self, ref: str) -> tuple[str, str]:
        resp = self._call("GET", f"/contents/{self.path}", params={"ref": ref})
        resp.raise_for_status()
        body = resp.json()
        return base64.b64decode(body["content"]).decode(), body["sha"]

    def history(self, depth: int) -> Iterator[Revision]:
        """images.yaml을 바꾼 커밋을 최신부터. 파일 내용은 꺼낼 때 하나씩 읽는다. 형식이 깨진 옛 커밋은 건너뛴다."""
        resp = self._call("GET", "/commits", params={"path": self.path, "sha": self.branch, "per_page": depth})
        resp.raise_for_status()
        for c in resp.json():
            try:
                images = parse_images(self._file(c["sha"])[0])
            except ImagesFileError as e:
                log.warning("images.yaml at %s skipped: %s", c["sha"][:7], e)
                continue
            yield Revision(c["sha"], datetime.fromisoformat(c["commit"]["committer"]["date"]), c["commit"]["message"], images)

    def current_images(self) -> dict[str, str]:
        return parse_images(self._file(self.branch)[0])

    def open_rollback_pr(self, branch: str, app: str, image: str, title: str, body: str) -> PullRequest:
        """같은 branch로 다시 불러도 PR은 하나다. 중간에 죽었으면 남은 단계만 한다."""
        existing = self._find_pr(branch)
        if existing:
            return existing
        base = self._call("GET", f"/git/ref/heads/{self.branch}").raise_for_status().json()["object"]["sha"]
        resp = self._call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base})
        if resp.status_code != 422:  # 422: branch가 이미 있다(이전 시도가 만들었다)
            resp.raise_for_status()
        text, blob = self._file(branch)
        if parse_images(text).get(app) != image:
            self._call("PUT", f"/contents/{self.path}", json={
                "message": title, "branch": branch, "sha": blob,
                "content": base64.b64encode(set_image(text, app, image).encode()).decode(),
            }).raise_for_status()
        resp = self._call("POST", "/pulls", json={"title": title, "head": branch, "base": self.branch, "body": body})
        if resp.status_code == 422 and (pr := self._find_pr(branch)):
            return pr
        resp.raise_for_status()
        return _pr(resp.json())

    def _find_pr(self, branch: str) -> PullRequest | None:
        owner = self.repo.split("/")[0]
        resp = self._call("GET", "/pulls", params={"head": f"{owner}:{branch}", "state": "all"})
        resp.raise_for_status()
        pulls = resp.json()
        return _pr(pulls[0]) if pulls else None

    def pull(self, number: int) -> PullRequest:
        return _pr(self._call("GET", f"/pulls/{number}").raise_for_status().json())

    def merge(self, number: int, head_sha: str, title: str) -> tuple[bool, str]:
        """head_sha가 다르면 GitHub가 거절한다. 제안 뒤 누가 PR에 커밋을 더했으면 merge하지 않는다."""
        resp = self._call("PUT", f"/pulls/{number}/merge",
                          json={"sha": head_sha, "merge_method": "squash", "commit_title": title})
        if resp.status_code == 200:
            return True, resp.json().get("sha", "")
        return False, f"HTTP {resp.status_code}: {resp.json().get('message', resp.text)[:300]}"


def make_infra_repo(settings) -> InfraRepo | None:
    s = settings
    if not (s.github_app_id and s.github_app_installation_id and s.github_app_private_key):
        return None
    http = httpx.Client(base_url=API, timeout=10)
    auth = GitHubAppAuth(s.github_app_id, s.github_app_installation_id, s.github_app_private_key, http)
    return InfraRepo(s.infra_repo, s.infra_branch, s.images_path, auth, http)
