"""SERVICE 레포 소스를 커밋별로 한 번 받아 로컬에 풀어 둔다. 원인 분석의 코드 근거, 수정안 생성, diff와 영향 범위 분석이 읽는다.

코드 읽기 App(SERVICE 하나에 Contents 읽기, Metadata 읽기)으로 tarball을 받는다. git clone도 쓰기 권한도 없다.
"""
import io
import logging
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx

from app.sources.github import API, GitHubAppAuth

log = logging.getLogger(__name__)
# 영향 범위와 코드 근거는 Python만 본다. 다른 파일은 풀지 않는다(frontend TS는 나중에)
SOURCE_SUFFIXES = (".py",)


class ServiceSource:
    def __init__(self, repo: str, ref: str, auth: GitHubAppAuth, http: httpx.Client, cache_dir: str):
        self.repo, self.ref, self.auth, self.http, self.cache = repo, ref, auth, http, Path(cache_dir)

    def _get(self, url: str, accept: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.auth.token()}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        # tarball은 codeload로 redirect된다. httpx는 다른 호스트로 넘어갈 때 Authorization을 떼고 보낸다
        return self.http.get(f"/repos/{self.repo}{url}", headers=headers, follow_redirects=True).raise_for_status()

    def resolve(self) -> str:
        """ref(기본 main)가 지금 가리키는 커밋. 한 장애의 단계들은 이 커밋 하나를 본다."""
        # ponytail: 배포된 이미지의 커밋이 아니라 main을 본다. 이미지 PR이 SERVICE 커밋을 남기면(SPEC images-yaml) 그 커밋으로 바꾼다
        return self._get(f"/commits/{self.ref}", "application/vnd.github.sha").text.strip()

    def checkout(self, sha: str, subdir: str) -> Path:
        """sha의 subdir(backend 등)을 풀어 둔 경로. 이미 있으면 다시 받지 않는다."""
        dest = self.cache / sha / subdir
        if dest.is_dir():
            return dest
        data = self._get(f"/tarball/{sha}", "application/vnd.github+json").content
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=self.cache))
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                # tarball 맨 위 디렉터리는 owner-repo-sha7/이다. subdir 아래 소스 파일만 푼다
                members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(SOURCE_SUFFIXES)
                           and m.name.split("/")[1:2] == [subdir]]
                tar.extractall(tmp, members=members, filter="data")
            src = next(tmp.iterdir(), tmp) / subdir
            src.mkdir(parents=True, exist_ok=True)  # subdir에 소스가 없어도 빈 디렉터리로 둔다
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(src, dest)
            except OSError:
                if not dest.is_dir():  # 다른 워커가 먼저 풀었으면 그것을 쓴다
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return dest


def make_service_source(settings) -> ServiceSource | None:
    s = settings
    if not (s.code_app_id and s.code_app_installation_id and s.code_app_private_key):
        return None
    http = httpx.Client(base_url=API, timeout=30)
    auth = GitHubAppAuth(s.code_app_id, s.code_app_installation_id, s.code_app_private_key, http)
    return ServiceSource(s.service_repo, s.service_ref, auth, http, s.source_cache_dir)
