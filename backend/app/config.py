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

    # --- Background worker and job queue ---
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

    # --- Automatic comment discovery ---
    discovery_poll_seconds: int = 300
    discovery_max_pages_per_poll: int = 2

    # --- Model stages ---
    # Must stay at least 15 seconds below job_lease_seconds (checked at worker startup).
    llm_stage_timeout_seconds: float = 95
    # Run every execution path through the shared stage pipeline (False restores the
    # original sequential path for the interactive UI).
    stage_pipeline_shared: bool = True

    # --- Shared budgets, enforced atomically across all workers (0 disables one) ---
    # LLM defaults sit safely under Groq's documented free-plan limits
    # (30 requests/minute, 1000 requests/day).
    llm_requests_per_minute: int = 28
    llm_requests_per_day: int = 900
    # Daily token ceilings per model role for the usage gauge and the replies-remaining
    # estimate (teammate Hrishik1033, PR #3). Local ceilings set to match the provider
    # plan (e.g. Groq's published per-model limits); not read from, or enforced by, the
    # provider. 0 leaves the role uncapped.
    llm_primary_tokens_per_day: int = 200_000
    llm_secondary_tokens_per_day: int = 200_000
    # "per_attempt" charges every HTTP attempt, including provider-level retries.
    llm_budget_mode: Literal["per_attempt", "off"] = "per_attempt"
    llm_budget_ui: bool = False
    # YouTube Data API reads made by the worker (comment lookup, video context,
    # reconciliation).
    youtube_reads_per_minute: int = 30
    youtube_units_per_day_budget: int = 8000
    # Automatic publication caps (per channel owner).
    auto_publish_max_per_hour: int = 10
    auto_publish_max_per_day: int = 50

    # --- Guardrails ---
    # Re-read the source comment immediately before publishing (edited/deleted/answered).
    revalidate_before_publish: bool = True
    # route_high_risk: hidden-character concealment blocks automatic publication.
    input_signals_mode: Literal["off", "observe", "route_high_risk"] = "route_high_risk"
    reply_policy_mode: Literal["off", "enforce_auto"] = "enforce_auto"
    language_contract: bool = True
    # Agent-directed instructions in a comment (override, fake system/creator/verifier
    # messages, dictated replies, credential requests) send it to a person.
    injection_routing: Literal["off", "review"] = "review"
    # "always": a creator-only question goes to a person whatever the grounding variant.
    answerability_routing: Literal["grounded_only", "always"] = "always"
    # "model_and_cues": deterministic sarcasm cues also route, unless a classifier said no.
    sarcasm_routing: Literal["model_only", "model_and_cues"] = "model_and_cues"
    # Account compromise, money disputes, legal threats, self-harm: always a person.
    sensitive_topic_routing: Literal["off", "review"] = "review"
    # Replace a generated draft with a holding reply when the policy already holds it for a
    # creator-only question, sarcasm read literally, or a dictated reply (no model call).
    reply_guard: bool = True

    # --- Experiment switches (baseline arms stay reproducible by setting them) ---
    # Video evidence given to the models: v0 = triage only sees the video; v1 (default)
    # also gives the generator the evidence pack and tells it not to guess creator-only
    # facts; v2 adds a grounding-aware quality review.
    grounding_variant: Literal["v0", "v1", "v2", "v3"] = "v1"
    # Minimum triage confidence for an automatic skip; below it the comment goes to a
    # person instead. 0 = always trust the triage category.
    triage_skip_threshold: float = 0.0
    # Remove concealment characters from the text sent to the models.
    sanitize_model_input: bool = False
    # Wrap untrusted comment text in a per-request random delimiter.
    spotlight_untrusted: bool = False
    # "tiered" skips model calls that cannot change the outcome.
    routing_mode: Literal["fixed", "tiered"] = "fixed"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
