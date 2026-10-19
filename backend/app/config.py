from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ReplyAI API"
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://replyai:replyai_dev_only@127.0.0.1:5432/replyai"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    frontend_base_url: str = "http://127.0.0.1:5173"
    app_environment: str = "development"
    draft_workflow_engine: Literal["legacy", "langgraph"] = "legacy"
    google_oauth_client_secrets_file: str = ""
    # Must match an authorized redirect URI on the Google OAuth client exactly.
    google_redirect_uri: str = "http://127.0.0.1:8001/auth/google/callback"
    token_encryption_key: SecretStr = SecretStr("")
    primary_llm_base_url: str = ""
    primary_llm_api_key: SecretStr = SecretStr("")
    primary_llm_model: str = ""
    secondary_llm_base_url: str = ""
    secondary_llm_api_key: SecretStr = SecretStr("")
    secondary_llm_model: str = ""
    session_cookie_name: str = "replyai_session"
    session_ttl_seconds: int = 1_209_600
    oauth_state_ttl_seconds: int = 600

    # --- Background batch processing (Phase 4) ---
    # A claimed job renews a lease while it runs; an expired lease means the worker
    # died and the job becomes claimable again.
    job_lease_seconds: int = 120
    job_max_attempts: int = 3
    # Recovery passes are capped separately from provider retries so a crash loop cannot
    # burn the retry budget, and a retry storm cannot hide a crash loop.
    job_max_recoveries: int = 3
    job_poll_interval_seconds: float = 1.0
    # Per-channel concurrent processing cap, derived from live leases (self-healing:
    # a dead worker's lease expires and frees its slot).
    worker_max_jobs_per_channel: int = 2
    # Shared provider budgets, enforced atomically across all workers. 0 disables a
    # budget. Defaults sit safely under Groq's documented free-plan limits
    # (30 requests/minute, 1000 requests/day).
    llm_requests_per_minute: int = 28
    llm_requests_per_day: int = 900
    # YouTube Data API reads made by the worker (comment lookup, video context,
    # reconciliation). 0 disables.
    youtube_reads_per_minute: int = 30
    # Daily automatic-publication budget, in addition to the existing per-hour limit.
    auto_publish_max_per_hour: int = 10
    auto_publish_max_per_day: int = 50

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
