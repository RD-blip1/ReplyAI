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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
