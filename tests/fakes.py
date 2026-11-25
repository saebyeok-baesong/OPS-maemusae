"""경계 밖 시스템(GitHub, Discord, Loki, Kubernetes)의 가짜. HTTP 경계에서 바꿔 끼워 실제 클라이언트 코드를 그대로 지나가게 한다."""
import base64
import difflib
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.sources.github import GitHubAppAuth, InfraRepo
from app.sources.kube import Kube
from app.sources.loki import LokiClient

OWNER, REPO = "saebyeok-baesong", "INFRA-watch-nyang"
PATH = "gitops/apps/images.yaml"


def digest(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def image(app: str, tag: str) -> str:
    return f"123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/{app}@sha256:{digest(tag)}"


def images_yaml(**apps: str) -> str:
    """SPEC 계약대로 앱 줄 사이에 빈 줄을 둔다."""
    lines = ["# 앱 이름: 이미지@digest. 앱 레포 CI가 PR로 자기 줄을 고친다"]
    lines += [f"{app}: {img}" for app, img in apps.items()]
    return "\n\n".join(lines) + "\n"


@dataclass
class Commit:
    sha: str
    date: datetime
    message: str
    text: str


@dataclass
class FakeGitHub:
    """INFRA 레포 하나. 브랜치는 커밋 목록(최신부터)이다."""

    branches: dict[str, list[Commit]] = field(default_factory=dict)
    pulls: list[dict] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    tokens_issued: int = 0
    # 브랜치에 images.yaml 말고 더 바뀐 파일(이름, 상태). 테스트가 섞인 PR을 만들 때 쓴다
    extra_files: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def commit(self, branch: str, text: str, message: str, date: datetime) -> Commit:
        c = Commit(hashlib.sha1(f"{branch}{text}{message}{date}".encode()).hexdigest(), date, message, text)
        self.branches.setdefault(branch, []).insert(0, c)
        return c

    def _find(self, ref: str) -> Commit:
        if ref in self.branches:
            return self.branches[ref][0]
        return next(c for commits in self.branches.values() for c in commits if c.sha == ref)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if path.startswith("/app/installations/"):
            self.tokens_issued += 1
            return httpx.Response(201, json={"token": "inst-token",
                                             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        assert request.headers["Authorization"] == "Bearer inst-token"
        prefix = f"/repos/{OWNER}/{REPO}"
        path = path.removeprefix(prefix)
        params = request.url.params
        if method == "GET" and path == "/commits":
            commits = self.branches[params["sha"]][: int(params["per_page"])]
            return httpx.Response(200, json=[{"sha": c.sha, "commit": {"message": c.message,
                                              "committer": {"date": c.date.isoformat()}}} for c in commits])
        if path == f"/contents/{PATH}":
            if method == "GET":
                c = self._find(params["ref"])
                return httpx.Response(200, json={"content": base64.b64encode(c.text.encode()).decode(),
                                                 "sha": hashlib.sha1(c.text.encode()).hexdigest()})
            head = self.branches[body["branch"]][0]
            if body["sha"] != hashlib.sha1(head.text.encode()).hexdigest():
                return httpx.Response(409, json={"message": "sha mismatch"})
            self.commit(body["branch"], base64.b64decode(body["content"]).decode(), body["message"], datetime.now(timezone.utc))
            return httpx.Response(200, json={})
        if method == "GET" and path.startswith("/git/ref/heads/"):
            return httpx.Response(200, json={"object": {"sha": self.branches[path.rsplit("/", 1)[1]][0].sha}})
        if method == "POST" and path == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.branches:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.branches[name] = [self._find(body["sha"])]
            return httpx.Response(201, json={})
        if method == "GET" and path == "/pulls":
            branch = params["head"].split(":", 1)[1]
            return httpx.Response(200, json=[self._pr_json(p) for p in self.pulls if p["head"] == branch])
        if method == "POST" and path == "/pulls":
            if any(p["head"] == body["head"] and p["state"] == "open" for p in self.pulls):
                return httpx.Response(422, json={"message": "A pull request already exists"})
            self.pulls.append({"number": len(self.pulls) + 1, "head": body["head"], "base": body["base"], "title": body["title"],
                               "body": body["body"], "state": "open", "merged": False})
            return httpx.Response(201, json=self._pr_json(self.pulls[-1]))
        if method == "GET" and path.startswith("/pulls/") and path.endswith("/files"):
            pr = self.pulls[int(path.split("/")[2]) - 1]
            return httpx.Response(200, json=self._files_json(pr["head"]))
        if path.startswith("/pulls/"):
            pr = self.pulls[int(path.split("/")[2]) - 1]
            if method == "GET":
                return httpx.Response(200, json=self._pr_json(pr))
            if self.branches[pr["head"]][0].sha != body["sha"]:
                return httpx.Response(409, json={"message": "Head branch was modified"})
            self.commit(pr["base"], self.branches[pr["head"]][0].text, body["commit_title"], datetime.now(timezone.utc))
            pr.update(state="closed", merged=True)
            return httpx.Response(200, json={"sha": self.branches[pr["base"]][0].sha, "merged": True})
        return httpx.Response(404, json={"message": f"fake github: {method} {path}"})

    def _pr_json(self, pr: dict) -> dict:
        return {"number": pr["number"], "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{pr['number']}",
                "state": pr["state"], "merged": pr["merged"], "head": {"sha": self.branches[pr["head"]][0].sha},
                "base": {"ref": pr["base"]}}

    def _files_json(self, branch: str) -> list[dict]:
        """브랜치를 만든 커밋(목록의 맨 끝)과 지금 머리의 diff. GitHub처럼 ---, +++ 머리줄 없이 hunk만 준다."""
        commits = self.branches[branch]
        before, after = commits[-1].text, commits[0].text
        out = []
        if before != after:
            patch = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))[2:]
            out.append({"filename": PATH, "status": "modified", "patch": "\n".join(patch)})
        return out + [{"filename": name, "status": status, "patch": "@@ -0,0 +1 @@\n+x"}
                      for name, status in self.extra_files.get(branch, [])]

    def main_images_text(self) -> str:
        return self.branches["main"][0].text

    def repo(self, private_key: str) -> InfraRepo:
        http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(self.handle))
        return InfraRepo(f"{OWNER}/{REPO}", "main", PATH, GitHubAppAuth("12345", "678", private_key, http), http)


class FakeDiscord:
    def __init__(self):
        self.posts: list[tuple[str, dict, str | None]] = []
        self.edits: list[tuple[str, str, dict]] = []

    def post(self, channel_id: str, message: dict, reply_to: str | None = None) -> str:
        self.posts.append((channel_id, message, reply_to))
        return f"m{len(self.posts)}"

    def edit(self, channel_id: str, message_id: str, changes: dict) -> None:
        self.edits.append((channel_id, message_id, changes))


TRACE = ('Traceback (most recent call last):\n  File "/app/app/api/routes/orders.py", line 88, in list_orders\n'
         '    total = sum(o.amount for o in orders) / len(orders)\nZeroDivisionError: division by zero')
REQUEST_ID = "3f2c9a7b1e4d4c5fa0b1c2d3e4f5a6b7"


def loki(exc_lines: list[dict] | None = None, pod_lines: list[str] | None = None, fail: bool = False):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        seen.append(query)
        if fail:
            return httpx.Response(503)
        lines = [json.dumps(x) for x in exc_lines or []] if "app.error" in query else list(pod_lines or [])
        base = 1795000000000000000
        values = [[str(base + i), line] for i, line in enumerate(lines)]
        return httpx.Response(200, json={"data": {"result": [{"stream": {}, "values": values}]}})

    return LokiClient("http://loki", transport=httpx.MockTransport(handle)), seen


def kube(events: list[dict]) -> Kube:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": events})

    return Kube(httpx.Client(base_url="https://k8s", transport=httpx.MockTransport(handle)))


# SERVICE 레포 backend의 축소판. TRACE가 가리키는 orders.py 88번째 줄에서 0으로 나눈다
SERVICE_SHA = "5e7c1f0" + "0" * 33
ORDERS = "\n" * 84 + (
    "def list_orders(orders):\n"
    "    if orders is None:\n"
    "        return 0\n"
    "    total = sum(o.amount for o in orders) / len(orders)\n"
    "    return total\n"
)
SERVICE_FILES = {
    "backend/app/__init__.py": "",
    "backend/app/api/__init__.py": "",
    "backend/app/api/routes/__init__.py": "",
    "backend/app/api/routes/orders.py": ORDERS,
    "backend/app/api/routes/reports.py": "from app.api.routes.orders import list_orders\n\n\ndef monthly(o):\n    return list_orders(o)\n",
    "backend/app/api/routes/health.py": "from app.api.routes import orders\n\n\ndef ok():\n    return True\n",
    "backend/app/main.py": "from app.api.routes import reports\n\napp = reports\n",
    "backend/tests/test_orders.py": "from app.api.routes.orders import list_orders\n\n\ndef test_none():\n    assert list_orders(None) == 0\n",
    "backend/pyproject.toml": "[project]\nname = 'backend'\n",
    "frontend/src/page.tsx": "export default function Page() {}\n",
}
# orders.py의 list_orders 한 줄만 고친다. reports(list_orders를 씀)와 main, 테스트가 영향 범위다
FIX_PATCH = """--- a/app/api/routes/orders.py
+++ b/app/api/routes/orders.py
@@ -86,3 +86,5 @@
     if orders is None:
         return 0
+    if not orders:
+        return 0
     total = sum(o.amount for o in orders) / len(orders)
"""


def tarball(files: dict[str, str], top: str) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class FakeServiceGitHub:
    """SERVICE 레포. main은 SERVICE_SHA 하나이고, tarball은 GitHub처럼 codeload로 redirect한다."""

    files: dict[str, str] = field(default_factory=lambda: dict(SERVICE_FILES))
    downloads: int = 0
    fail: bool = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/app/installations/"):
            return httpx.Response(201, json={"token": "code-token",
                                             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        if request.url.host == "codeload.github.com":
            assert "Authorization" not in request.headers, "다른 호스트로 토큰을 보내지 않는다"
            self.downloads += 1
            return httpx.Response(200, content=tarball(self.files, f"{OWNER}-SERVICE-watch-nyang-{SERVICE_SHA[:7]}"))
        assert request.headers["Authorization"] == "Bearer code-token"
        if self.fail:
            return httpx.Response(503)
        path = path.removeprefix(f"/repos/{OWNER}/SERVICE-watch-nyang")
        if path == "/commits/main":
            return httpx.Response(200, text=SERVICE_SHA)
        if path == f"/tarball/{SERVICE_SHA}":
            return httpx.Response(302, headers={"Location": f"https://codeload.github.com/{OWNER}/SERVICE-watch-nyang/legacy.tar.gz/{SERVICE_SHA}?token=t"})
        return httpx.Response(404)

    def source(self, private_key: str, cache_dir: str):
        from app.sources.service import ServiceSource

        http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(self.handle))
        return ServiceSource(f"{OWNER}/SERVICE-watch-nyang", "main", GitHubAppAuth("999", "111", private_key, http), http, cache_dir)
