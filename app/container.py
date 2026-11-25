"""의존성 조립. api, worker, approvals 세 진입점이 같은 구성 요소를 공유한다. 테스트는 속성을 바꿔 끼운다."""
from collections.abc import Callable
from functools import cached_property
from typing import Any

from app.config import Settings, get_settings
from app.core.queue import IncidentQueue
from app.core.store import IncidentStore
from app.db import checkpointer, make_pool
from app.discord.client import DiscordClient
from app.infra import make_redis
from app.llm.cost import CostTracker
from app.llm.router import LLMRouter, default_model_factory
from app.metrics import Metrics
from app.rag.embedder import CachedEmbedder, build_embedder
from app.rag.ingest import IncrementalIngestor
from app.rag.retriever import Retriever
from app.security.store import SecurityStore


class Container:
    def __init__(self, settings: Settings | None = None, model_factory: Callable[[str], Any] | None = None):
        self.settings = settings or get_settings()
        self.model_factory = model_factory or default_model_factory

    @cached_property
    def redis(self):
        return make_redis(self.settings.redis_url)

    @cached_property
    def db(self):
        return make_pool(self.settings.database_url)

    @cached_property
    def security_db(self):
        return make_pool(self.settings.security_database_url)

    @cached_property
    def queue(self) -> IncidentQueue:
        s = self.settings
        return IncidentQueue(self.redis, s.queue_stream, s.queue_group, s.queue_dlq_stream)

    @cached_property
    def metrics(self) -> Metrics:
        return Metrics(self.queue)

    @cached_property
    def store(self) -> IncidentStore:
        return IncidentStore(self.db, self.redis)

    @cached_property
    def security(self) -> SecurityStore:
        return SecurityStore(self.security_db)

    @cached_property
    def embedder(self) -> CachedEmbedder:
        return build_embedder(self.settings.embedding_provider, self.settings.embedding_model, self.redis)

    @cached_property
    def ingestor(self) -> IncrementalIngestor:
        return IncrementalIngestor(self.db, self.embedder, self.redis)

    @cached_property
    def retriever(self) -> Retriever:
        return Retriever(self.db, self.embedder, self.redis, self.settings.retrieval_cache_ttl_sec)

    @cached_property
    def cost(self) -> CostTracker:
        return CostTracker(self.db, self.settings.model_prices, self.settings.incident_budget_usd)

    @cached_property
    def router(self) -> LLMRouter:
        s = self.settings
        return LLMRouter(s.light_model, s.heavy_model, self.redis, self.cost, s.llm_cache_ttl_sec, self.model_factory)

    @cached_property
    def loki(self):
        from app.sources.loki import LokiClient

        return LokiClient(self.settings.loki_url) if self.settings.loki_url else None

    @cached_property
    def kube(self):
        from app.sources.kube import Kube, in_cluster

        return Kube() if in_cluster() else None

    @cached_property
    def infra(self):
        """INFRA 레포. GitHub App 설정이 없으면 None이고, 배포 이력과 롤백 제안이 빠진다."""
        if self.settings.infra_repo_backend == "local-git":
            from app.sources.local_git import make_local_infra_repo

            return make_local_infra_repo(self.settings)
        from app.sources.github import make_infra_repo

        return make_infra_repo(self.settings)

    @cached_property
    def source(self):
        """SERVICE 소스. 코드 읽기 App 설정이 없으면 None이고, 코드 근거와 수정안이 빠진다."""
        from app.sources.service import make_service_source

        return make_service_source(self.settings)

    @cached_property
    def discord(self) -> DiscordClient:
        return DiscordClient(self.settings.discord_bot_token)

    @cached_property
    def graph(self):
        from app.graph.nodes import Nodes
        from app.graph.workflow import build_graph

        return build_graph(Nodes(self.settings, self.router, self.retriever, self.cost, self.source),
                           self.settings.fix_max_attempts, checkpointer=checkpointer(self.db))

    @cached_property
    def digest(self):
        from app.security.digest import SecurityDigest

        s = self.settings
        return SecurityDigest(self.security, self.router, self.discord, s.discord_security_channel_id, s.security_report_hour_kst)
