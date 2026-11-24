import pytest

from app.llm.cost import BudgetExceeded
from app.llm.router import default_model_factory
from app.llm.schemas import Classification, RootCause


def test_tasks_go_to_their_tier_and_cache(container, llm_calls):
    r = container.router
    r.invoke("classify", Classification, "s", "OOMKilled", subject="i1", cache_key="fp")
    r.invoke("classify", Classification, "s", "OOMKilled", subject="i1", cache_key="fp")
    r.invoke("root_cause", RootCause, "s", "u", subject="i1")
    assert llm_calls == [("fake:light", "Classification"), ("fake:heavy", "RootCause")]
    summary = container.cost.summary("i1")
    assert summary["cache_hits"] == 1 and summary["usd"] > 0, "단가표로 비용을 계산한다"


def test_budget_is_checked_before_each_call(container):
    container.settings.incident_budget_usd = 0.0
    container.__dict__.pop("cost", None)
    container.__dict__.pop("router", None)
    with pytest.raises(BudgetExceeded):
        container.router.invoke("root_cause", RootCause, "s", "u", subject="i2")


def test_model_names_need_a_provider_prefix(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ValueError):
        default_model_factory("gpt-4.1")
    assert default_model_factory("openai:any-model").model_name == "any-model"
