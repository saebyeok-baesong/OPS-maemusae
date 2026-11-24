"""환경변수 설정. 로컬은 .env, 클러스터는 Secret과 ConfigMap으로 넣는다."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 접수. 토큰을 둘로 나눈 이유: CI(GitHub Secrets)에 있는 보안 보고 토큰이 새도 장애를 위조할 수 없게 한다
    alertmanager_token: str = "change-me"
    security_report_token: str = "change-me-too"
    dedupe_ttl_sec: int = 600

    # 큐
    redis_url: str = "redis://localhost:6379/0"
    queue_stream: str = "incidents"
    queue_group: str = "ops-workers"
    queue_dlq_stream: str = "incidents:dlq"
    queue_claim_idle_ms: int = 15 * 60 * 1000

    # RDS 두 개. incident: 장애, 런북 벡터, LLM 호출, 조치, 체크포인트. security: 보안 결과와 일일 보고
    database_url: str = "postgresql://ops:ops@localhost:55432/incident"
    security_database_url: str = "postgresql://ops:ops@localhost:55432/security"

    # 임베딩. documents.embedding이 vector(384)라서 모델을 바꾸면 마이그레이션이 필요하다
    embedding_provider: str = "fastembed"  # fastembed | hash(테스트용)
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    # LLM. 모델 이름과 단가는 기본값을 쓰기 전에 OpenAI 모델 목록과 요금표에서 확인한다
    light_model: str = "openai:gpt-4.1-mini"
    heavy_model: str = "openai:gpt-4.1"
    # 100만 토큰당 USD [입력, 출력]. 표에 없는 모델은 0으로 계산되어 예산 상한이 듣지 않는다
    model_prices: dict[str, list[float]] = Field(
        default_factory=lambda: {"openai:gpt-4.1-mini": [0.40, 1.60], "openai:gpt-4.1": [2.0, 8.0]}
    )
    llm_cache_ttl_sec: int = 86400
    incident_budget_usd: float = 0.5

    # 런북 검색
    retrieval_cache_ttl_sec: int = 600
    retrieval_top_k: int = 5
    retrieval_lookback_days: int = 365
    context_chunk_max_chars: int = 1200
    runbook_base_url: str = "https://github.com/saebyeok-baesong/OPS-maemusae/blob/main/knowledge"

    # 근거 수집. 비우면 그 근거 없이 접수한다
    loki_url: str = ""

    # Discord. 카드 게시와 승인 버튼을 봇 하나가 맡는다
    discord_bot_token: str = ""
    discord_incident_channel_id: str = ""
    discord_security_channel_id: str = ""
    # 승인할 수 있는 Discord 역할 ID(인프라팀). 비우면 아무도 승인하지 못한다
    discord_approver_role_ids: list[str] = []

    # 롤백(INFRA 레포 PR)
    github_app_id: str = ""
    github_app_installation_id: str = ""
    github_app_private_key: str = ""
    infra_repo: str = "saebyeok-baesong/INFRA-maemusae"
    infra_branch: str = "main"
    images_path: str = "gitops/apps/images.yaml"
    rollback_apps: list[str] = ["web", "api"]
    rollback_window_min: int = 30
    deploy_history_depth: int = 20

    # 보안 일일 보고(한국 시각)
    security_report_hour_kst: int = 9


@lru_cache
def get_settings() -> Settings:
    return Settings()
