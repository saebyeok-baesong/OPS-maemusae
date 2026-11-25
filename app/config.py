"""환경변수 설정. 로컬은 .env, 클러스터는 Secret과 ConfigMap으로 넣는다."""
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 접수. 토큰을 둘로 나눈 이유: kube-bench Job에 있는 보안 보고 토큰이 새도 장애를 위조할 수 없게 한다
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

    # Discord. 메시지 게시와 승인 버튼을 봇 하나가 맡는다
    discord_bot_token: str = ""
    discord_incident_channel_id: str = ""
    discord_security_channel_id: str = ""
    # 승인할 수 있는 Discord 역할 ID. 쉼표로 나눈다. 이 중 하나라도 가진 사람이 승인한다. 비우면 아무도 승인하지 못한다
    discord_approver_role_ids: Annotated[list[str], NoDecode] = []

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

    # 코드 수정안(원인 분석, 수정 코드 생성, diff, 영향 범위). 코드 읽기 App은 SERVICE 하나에 Contents, Metadata 읽기만 준다.
    # 비우면 코드 없이 원인 분석에서 끝난다
    code_app_id: str = ""
    code_app_installation_id: str = ""
    code_app_private_key: str = ""
    service_repo: str = "saebyeok-baesong/SERVICE-maemusae"
    service_ref: str = "main"
    # 앱 -> SERVICE 레포 안 소스 디렉터리. 영향 범위가 Python만 보므로 지금은 api만. web(frontend, TS)은 나중에 더한다
    service_source_dirs: dict[str, str] = Field(default_factory=lambda: {"api": "backend"})
    source_cache_dir: str = "/tmp/ops-source"
    # 수정안이 diff 형식이 아니면 실패 사유를 붙여 다시 만든다. 이 횟수까지
    fix_max_attempts: int = 2

    # 보안 일일 보고(한국 시각)
    security_report_hour_kst: int = 9
    # GitHub Actions 스캔 결과가 올라오는 S3 버킷(maemusae-security-scans-<account_id>). 비우면 수집 Job이 돌지 않는다
    security_scan_bucket: str = ""

    @field_validator("discord_approver_role_ids", mode="before")
    @classmethod
    def _comma_separated(cls, v):
        return [r.strip() for r in v.split(",") if r.strip()] if isinstance(v, str) else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
