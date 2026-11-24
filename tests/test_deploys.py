from datetime import datetime, timedelta, timezone

import pytest

from app.deploys import ROLLBACK_MARK, ImagesFileError, Revision, deploy_from_history, parse_images, set_image
from tests.fakes import image, images_yaml

T0 = datetime(2026, 11, 24, tzinfo=timezone.utc)


def rev(n: int, message: str = "deploy", **apps: str) -> Revision:
    return Revision(f"c{n}", T0 + timedelta(hours=n), message, {a: image(a, t) for a, t in apps.items()})


def test_parse_accepts_only_digest_pinned_images():
    assert parse_images(images_yaml(web=image("web", "1"), api=image("api", "2"))) == {"web": image("web", "1"), "api": image("api", "2")}
    with pytest.raises(ImagesFileError, match="digest"):
        parse_images("api: 1234.dkr.ecr.ap-northeast-2.amazonaws.com/api:latest\n")
    with pytest.raises(ImagesFileError):
        parse_images("- api\n")


def test_set_image_changes_one_line_and_keeps_comments():
    text = images_yaml(web=image("web", "1"), api=image("api", "2")).replace("\napi:", "\n# api는 FastAPI\napi:")
    out = set_image(text, "api", image("api", "1"))
    assert out == text.replace(image("api", "2"), image("api", "1"))
    with pytest.raises(ImagesFileError):
        set_image(text, "worker", image("api", "1"))
    with pytest.raises(ImagesFileError):
        set_image(text, "api", "api:latest")


def test_deploy_is_the_newest_commit_that_changed_the_app():
    history = [rev(3, web="w2", api="a2"), rev(2, web="w1", api="a2"), rev(1, web="w1", api="a1")]
    api = deploy_from_history("api", history)
    assert (api.image, api.previous, api.commit, api.deployed_at) == (image("api", "a2"), image("api", "a1"), "c2", T0 + timedelta(hours=2))
    web = deploy_from_history("web", history)
    assert (web.commit, web.previous) == ("c3", image("web", "w1"))


def test_unknown_previous_and_unknown_app():
    assert deploy_from_history("api", [rev(2, api="a1"), rev(1, api="a1")]).previous is None
    assert deploy_from_history("worker", [rev(1, api="a1")]) is None
    assert deploy_from_history("api", []) is None


def test_history_is_read_only_until_the_change():
    read = []

    def lazy():
        for r in [rev(3, api="a2"), rev(2, api="a1"), rev(1, api="a0")]:
            read.append(r.commit)
            yield r

    deploy_from_history("api", lazy())
    assert read == ["c3", "c2"], "바뀐 커밋을 찾으면 더 오래된 파일은 읽지 않는다"


def test_bot_rollback_commit_is_marked():
    d = deploy_from_history("api", [rev(2, f"ops: api 롤백 {ROLLBACK_MARK} (#3)", api="a1"), rev(1, api="a2")])
    assert d.by_rollback and d.previous == image("api", "a2")
