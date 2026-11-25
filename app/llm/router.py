"""작업 난이도별 모델 분리 + 응답 캐시 + 비용 기록.

분류와 보안 요약은 경량 모델, 원인 분석은 고성능 모델이 맡는다. 분류는 에러 지문을 캐시 키로 써서
같은 에러가 반복되면 LLM을 다시 부르지 않는다.
"""
import hashlib
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel
from redis import Redis

from app.llm.cost import CostTracker

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class Tier(StrEnum):
    LIGHT = "light"
    HEAVY = "heavy"


TASK_TIER: dict[str, Tier] = {
    "classify": Tier.LIGHT,
    "security_summary": Tier.LIGHT,
    "root_cause": Tier.HEAVY,
    "generate_fix": Tier.HEAVY,
}
CACHEABLE_TASKS = {"classify", "root_cause"}


def default_model_factory(name: str) -> Any:
    provider, _, model = name.partition(":")
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, max_tokens=2048)
    if provider == "fake":  # API 키 없이 compose로 흐름만 볼 때 (LIGHT_MODEL=fake:light)
        from app.llm.fake import FakeChatModel, demo_responder

        return FakeChatModel(name, demo_responder, [])
    raise ValueError(f"unsupported model provider: {name}")


class LLMRouter:
    def __init__(self, light_model: str, heavy_model: str, redis: Redis, cost: CostTracker, cache_ttl: int,
                 model_factory: Callable[[str], Any] = default_model_factory):
        self.models = {Tier.LIGHT: light_model, Tier.HEAVY: heavy_model}
        self.redis = redis
        self.cost = cost
        self.cache_ttl = cache_ttl
        self.model_factory = model_factory
        self._clients: dict[str, Any] = {}

    def model_for(self, task: str) -> str:
        return self.models[TASK_TIER[task]]

    def _client(self, name: str) -> Any:
        if name not in self._clients:
            self._clients[name] = self.model_factory(name)
        return self._clients[name]

    def invoke(self, task: str, schema: type[T], system: str, user: str, subject: str, cache_key: str | None = None) -> T:
        model = self.model_for(task)
        key = None
        if task in CACHEABLE_TASKS:
            basis = cache_key or hashlib.sha256((system + "\n" + user).encode()).hexdigest()
            key = f"llm:{model}:{task}:{basis}"
            if hit := self.redis.get(key):
                self.cost.record_cache_hit(subject, task)
                log.info("llm cache hit task=%s", task)
                return schema.model_validate_json(hit)

        self.cost.check_budget(subject)
        runnable = self._client(model).with_structured_output(schema, include_raw=True, method="json_schema")
        out = runnable.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        parsed, raw = out["parsed"], out["raw"]
        if parsed is None:
            raise ValueError(f"LLM returned unparsable output for task={task}: {out.get('parsing_error')}")
        if isinstance(parsed, dict):
            parsed = schema.model_validate(parsed)

        usage = getattr(raw, "usage_metadata", None) or {}
        self.cost.record(subject, model, task, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        if key:
            self.redis.set(key, parsed.model_dump_json(), ex=self.cache_ttl)
        return parsed
