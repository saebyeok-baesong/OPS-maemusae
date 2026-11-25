"""코드 수정안: SERVICE 소스 받기, 수정 코드 생성, diff, 영향 범위."""
import pytest

from app.analysis import code_context
from app.analysis.diff import analyze_diff
from app.analysis.impact import ImportGraph, analyze_impact
from app.llm.fake import demo_responder, fake_model_factory
from app.llm.schemas import FixProposal, RootCause
from tests import fakes
from tests.fakes import FIX_PATCH, SERVICE_SHA, FakeServiceGitHub
from tests.test_pipeline import drain, evidence, post  # noqa: F401 - evidence는 fixture


@pytest.fixture
def service(tmp_path, private_key):
    gh = FakeServiceGitHub()
    return gh, gh.source(private_key, str(tmp_path / "src"))


@pytest.fixture
def backend(service):
    _, src = service
    return str(src.checkout(src.resolve(), "backend"))


def test_checkout_unpacks_only_backend_python_once(service):
    gh, src = service
    sha = src.resolve()
    root = src.checkout(sha, "backend")
    again = src.checkout(sha, "backend")

    assert sha == SERVICE_SHA and root == again and gh.downloads == 1
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert "app/api/routes/orders.py" in files and "tests/test_orders.py" in files
    assert not any(f.endswith((".toml", ".tsx")) for f in files), "Python 소스만 푼다"


def test_diff_finds_the_changed_function_and_impact_follows_only_its_users(backend):
    diff = analyze_diff(FIX_PATCH, backend)
    [f] = diff.files
    assert (f.path, f.added, f.removed, f.changed_symbols) == ("app/api/routes/orders.py", 2, 0, ["list_orders"])

    impact = analyze_impact(diff, ImportGraph(backend))
    assert impact.changed_modules == ["app.api.routes.orders"]
    assert impact.impacted == {"app.api.routes.reports": 1, "tests.test_orders": 1, "app.main": 2}
    assert "app.api.routes.health" not in impact.impacted, "orders를 import해도 list_orders를 안 쓰면 뺀다"
    assert impact.risk == "low"


def test_signature_change_makes_every_importer_impacted_and_risk_high(backend):
    patch = """--- a/app/api/routes/orders.py
+++ b/app/api/routes/orders.py
@@ -85,1 +85,1 @@
-def list_orders(orders):
+def list_orders(orders, page=1):
"""
    diff = analyze_diff(patch, backend)
    assert diff.files[0].signature_changed == ["list_orders"]
    assert analyze_impact(diff, ImportGraph(backend)).risk == "high"


def test_paths_from_the_llm_cannot_read_outside_the_source(backend, tmp_path):
    (tmp_path / "secret.py").write_text("TOKEN = 'x'\n")
    assert code_context.from_files(backend, ["../../../secret.py", "/etc/passwd"]) == ""
    assert code_context.from_stacktrace(backend, fakes.TRACE).startswith("### app/api/routes/orders.py")


def with_llm(c, llm_calls, patch: str):
    def responder(schema, messages):
        if schema is RootCause:
            return RootCause(root_cause="빈 주문 목록에서 0으로 나눈다", evidence=["orders.py:88"],
                             suspected_area="list_orders", next_steps=["빈 목록을 처리한다"],
                             suspected_files=["app/api/routes/orders.py"])
        if schema is FixProposal:
            assert "  88|     total = sum" in str(messages[-1].content), "수정 대상 코드를 줄 번호와 같이 준다"
            return FixProposal(patch=patch, explanation="빈 목록이면 0을 돌려준다", risk="low")
        return demo_responder(schema, messages)

    c.model_factory = fake_model_factory(responder, llm_calls)
    return c


def test_code_bug_gets_a_fix_diff_and_impact_in_the_alert(evidence, webhook, llm_calls, service):  # noqa: F811
    c = with_llm(evidence, llm_calls, FIX_PATCH)
    c.__dict__["source"] = service[1]
    [incident_id] = post(c, webhook, 0)["accepted"]
    [final] = drain(c)

    assert final["outcome"] == "fix_proposed"
    assert llm_calls == [("fake:light", "Classification"), ("fake:heavy", "RootCause"), ("fake:heavy", "FixProposal")]
    embed = c.discord.posts[0][1]["embeds"][0]
    assert len(str(embed)) < 6000, "Discord embed 합계 한도"
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert "backend/app/api/routes/orders.py` +2 -0" in fields[f"수정안 (SERVICE `{SERVICE_SHA[:7]}`, 위험도 low)"]
    assert fields["영향 범위"].startswith("위험도 low · 바뀐 모듈 app.api.routes.orders · 영향 받는 모듈 2개, 테스트 1개")
    assert "+    if not orders:" in fields["diff"]
    record = c.store.get(incident_id)
    assert record.status == "fix_proposed" and record.result["impact"]["impacted"]["app.api.routes.reports"] == 1
    assert FIX_PATCH.strip() in final["report"]


def test_a_patch_that_is_not_a_diff_is_retried_then_dropped(evidence, webhook, llm_calls, service):  # noqa: F811
    c = with_llm(evidence, llm_calls, "list_orders에 빈 목록 검사를 넣는다")
    c.__dict__["source"] = service[1]
    post(c, webhook, 0)
    [final] = drain(c)

    assert final["outcome"] == "analyzed" and final["fix_attempts"] == 2
    assert [s for _, s in llm_calls].count("FixProposal") == 2
    fields = {f["name"]: f["value"] for f in c.discord.posts[0][1]["embeds"][0]["fields"]}
    assert fields["수정안"] == "2번 만들었지만 diff 형식이 아니어서 버렸다"


def test_without_the_service_source_it_stops_at_root_cause(evidence, webhook, llm_calls, service):  # noqa: F811
    gh, src = service
    gh.fail = True
    c = with_llm(evidence, llm_calls, FIX_PATCH)
    c.__dict__["source"] = src
    post(c, webhook, 0)
    [final] = drain(c)
    assert final["outcome"] == "analyzed" and "FixProposal" not in [s for _, s in llm_calls]
