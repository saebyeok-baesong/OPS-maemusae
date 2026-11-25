"""INFRA `gitops/apps/images.yaml` 계약(SPEC contracts/images-yaml.md)과 배포 이력.

파일은 앱 이름 하나에 `저장소@sha256:digest` 한 줄인 평평한 매핑이다. 배포 이력은 이 파일의 git 이력이다.
여기 함수는 모두 순수 함수다. GitHub에서 읽고 쓰는 일은 app/sources/github.py가 한다.
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import yaml

from app.incident import Deploy

IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_/:]*@sha256:[0-9a-f]{64}$")
APP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# 봇이 merge한 롤백 커밋의 표시. 롤백 직후 또 장애가 나도 '직전 이미지'(방금 되돌린 나쁜 이미지)로 다시 롤백하지 않는다
ROLLBACK_MARK = "[ops-rollback]"


class ImagesFileError(ValueError):
    pass


def parse_images(text: str) -> dict[str, str]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ImagesFileError("images.yaml은 앱 이름 -> 이미지 매핑이어야 한다")
    for app, image in data.items():
        if not (isinstance(app, str) and APP_RE.match(app)):
            raise ImagesFileError(f"앱 이름 형식이 아니다: {app!r}")
        if not (isinstance(image, str) and IMAGE_RE.match(image)):
            raise ImagesFileError(f"{app}의 이미지가 digest 고정 형식이 아니다: {image!r}")
    return data


def set_image(text: str, app: str, image: str) -> str:
    """그 앱의 줄 하나만 바꾼다. 주석과 나머지 줄의 모양은 그대로 둔다."""
    before = parse_images(text)
    if app not in before:
        raise ImagesFileError(f"images.yaml에 {app}이 없다")
    if not IMAGE_RE.match(image):
        raise ImagesFileError(f"digest 고정 형식이 아니다: {image!r}")
    pattern = re.compile(rf"^({re.escape(app)}\s*:\s*)(\S+)(.*)$", re.M)
    if len(pattern.findall(text)) != 1:
        raise ImagesFileError(f"{app} 줄이 한 줄이 아니다")
    after_text = pattern.sub(lambda m: f"{m.group(1)}{image}{m.group(3)}", text)
    if parse_images(after_text) != {**before, app: image}:
        raise ImagesFileError(f"{app} 줄만 바꾸지 못했다")
    return after_text


@dataclass(frozen=True)
class Revision:
    """images.yaml을 바꾼 커밋 하나. 이력은 최신 커밋부터."""

    commit: str
    committed_at: datetime
    message: str
    images: dict[str, str]


def deploy_from_history(app: str, history: Iterable[Revision]) -> Deploy | None:
    """지금 이미지가 처음 들어온 커밋을 배포 시각으로, 그 전 커밋의 이미지를 직전 이미지로 본다.

    history는 최신 커밋부터다. 바뀐 커밋을 찾으면 더 읽지 않는다(GitHub 호출이 대개 두 번으로 끝난다).
    """
    revisions = iter(history)
    newer = next(revisions, None)
    if newer is None or app not in newer.images:
        return None
    current = newer.images[app]
    for older in revisions:
        if older.images.get(app) != current:
            return Deploy(app=app, image=current, previous=older.images.get(app), deployed_at=newer.committed_at,
                          commit=newer.commit, by_rollback=ROLLBACK_MARK in newer.message)
        newer = older
    # 읽은 깊이 안에서 이미지가 한 번도 바뀌지 않았다. 직전 이미지를 모르므로 롤백 근거가 없다
    return Deploy(app=app, image=current, previous=None, deployed_at=newer.committed_at, commit=newer.commit,
                  by_rollback=ROLLBACK_MARK in newer.message)
