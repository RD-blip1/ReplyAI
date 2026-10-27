# Configuration

ReplyAI reads configuration from three files, each created from a tracked template:

| File | Template | Read by |
| --- | --- | --- |
| `.env` | `.env.example` | Docker Compose (PostgreSQL credentials, host ports) |
| `backend/.env` | `backend/.env.example` | The API, the worker and Alembic (`app/config.py`) |
| `frontend/.env.local` | `frontend/.env.example` | Vite (browser build) |

Backend settings are defined in `backend/app/config.py` with
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/). Every
field can be set in `backend/.env` or as an environment variable with the upper-case
name; environment variables win. Restart the API and the worker after changing them.

## Which integrations need credentials

| Feature | Needs |
| --- | --- |
| Simulation mode | Nothing beyond PostgreSQL |
| Connecting a YouTube channel | Google OAuth client JSON, `TOKEN_ENCRYPTION_KEY` |
| Reading comments and posting replies | The above, plus the `youtube.force-ssl` grant (requested in the UI) |
| AI drafting and verification | Both `PRIMARY_LLM_*` and `SECONDARY_LLM_*` roles |

Secrets (`DATABASE_URL`, `REDIS_URL`, `TOKEN_ENCRYPTION_KEY`, the LLM API keys) are
loaded as `SecretStr` and are never logged or returned by the API.

## Root `.env` (Docker Compose)

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_DB` | `replyai` | Database name |
| `POSTGRES_USER` | `replyai` | Database user |
| `POSTGRES_PASSWORD` | `replyai_dev_only` | Local-only password; never reuse it elsewhere |
| `POSTGRES_PORT` | `5432` | Host port for PostgreSQL |
| `REDIS_PORT` | `6379` | Host port for Redis |

Both services bind to `127.0.0.1` only.

## `backend/.env`

### Core

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://replyai:replyai_dev_only@127.0.0.1:5432/replyai` | Async SQLAlchemy URL |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Video-metadata cache; `/ready` reports it |
| `CORS_ORIGINS` | `["http://localhost:5173","http://127.0.0.1:5173"]` | Origins allowed to call the API and pass the CSRF origin check |
| `FRONTEND_BASE_URL` | `http://127.0.0.1:5173` | Where OAuth callbacks redirect the browser |
| `APP_ENVIRONMENT` | `development` | `production` marks cookies `Secure`; simulation requires `development` |
| `DRAFT_WORKFLOW_ENGINE` | `legacy` | `legacy` (sequential) or `langgraph` (LangGraph `StateGraph`) |

### Google OAuth

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` | empty | Absolute path to the Web-application OAuth client JSON, kept outside the repository |
| `GOOGLE_REDIRECT_URI` | `http://127.0.0.1:8001/auth/google/callback` | Must exactly match an authorized redirect URI on the client |
| `TOKEN_ENCRYPTION_KEY` | empty | Fernet key that encrypts stored Google tokens |

### LLM providers

Any OpenAI-compatible chat-completions API. The base URL is the API prefix; the adapter
appends `/chat/completions`.

| Variable | Purpose |
| --- | --- |
| `PRIMARY_LLM_BASE_URL`, `PRIMARY_LLM_API_KEY`, `PRIMARY_LLM_MODEL` | Generator (writes the reply) |
| `SECONDARY_LLM_BASE_URL`, `SECONDARY_LLM_API_KEY`, `SECONDARY_LLM_MODEL` | Verifier (triage, classification, quality review) |

Drafting is disabled until both roles are complete. Automatic publication additionally
requires the two roles to be **different models**.

### Worker and job queue

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOB_LEASE_SECONDS` | `120` | How long a claimed job is owned without a heartbeat |
| `JOB_MAX_ATTEMPTS` | `3` | Provider retries per job |
| `JOB_MAX_RECOVERIES` | `3` | Crash recoveries per job, counted separately from retries |
| `JOB_POLL_INTERVAL_SECONDS` | `1.0` | Worker sleep when the queue is idle |
| `WORKER_MAX_JOBS_PER_CHANNEL` | `2` | Concurrent jobs per channel across all workers |
| `LLM_STAGE_TIMEOUT_SECONDS` | `95` | Per-stage model timeout; must be at least 15 s below the lease |
| `STAGE_PIPELINE_SHARED` | `true` | Run every path through the shared stage pipeline |

### Discovery

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCOVERY_POLL_SECONDS` | `300` | Minimum interval between polls of one channel |
| `DISCOVERY_MAX_PAGES_PER_POLL` | `2` | Upper bound on comment pages read per poll |

### Budgets and caps

Shared across every API and worker process; `0` disables a budget.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_REQUESTS_PER_MINUTE` | `28` | Under Groq's free-plan 30 RPM |
| `LLM_REQUESTS_PER_DAY` | `900` | Under Groq's free-plan 1000 RPD |
| `LLM_BUDGET_MODE` | `per_attempt` | Charge every HTTP attempt, or `off` |
| `LLM_BUDGET_UI` | `false` | Also apply LLM budgets to interactive UI requests |
| `YOUTUBE_READS_PER_MINUTE` | `30` | Worker reads (comment lookups, context, reconciliation) |
| `YOUTUBE_UNITS_PER_DAY_BUDGET` | `8000` | Below the default 10,000-unit daily YouTube quota |
| `AUTO_PUBLISH_MAX_PER_HOUR` | `10` | Automatic replies per channel per hour |
| `AUTO_PUBLISH_MAX_PER_DAY` | `50` | Automatic replies per channel per day |

### Guardrails

| Variable | Default | Purpose |
| --- | --- | --- |
| `REVALIDATE_BEFORE_PUBLISH` | `true` | Re-read the comment immediately before publishing |
| `INPUT_SIGNALS_MODE` | `observe` | `off`, `observe` (record only) or `route_high_risk` (block automatic publication on concealment) |
| `REPLY_POLICY_MODE` | `enforce_auto` | `enforce_auto` blocks automatic publication on rule violations; `off` disables the rules |
| `LANGUAGE_CONTRACT` | `true` | Accept English, Hindi and Hinglish input; review when the model's language claim contradicts the script |

### Experimental switches

These are off by default and exist to compare behaviour; the defaults are the
established configuration.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GROUNDING_VARIANT` | `v0` | `v1`/`v2` give the models an evidence pack; `v2` also uses a grounding-aware quality review and routes creator-only questions to review |
| `TRIAGE_SKIP_THRESHOLD` | `0.0` | Minimum triage confidence for an automatic skip; lower-confidence skips go to a person |
| `SANITIZE_MODEL_INPUT` | `false` | Strip concealment characters from text sent to the models |
| `SPOTLIGHT_UNTRUSTED` | `false` | Wrap comment text in a random per-request delimiter |
| `ROUTING_MODE` | `fixed` | `tiered` skips model calls that cannot change the outcome |

### Development only

| Variable | Purpose |
| --- | --- |
| `REPLYAI_SIMULATION=1` | Fake YouTube and scripted models; requires `APP_ENVIRONMENT=development` and a database whose name ends in `_sim` |
| `REPLYAI_SIMULATION_STATE` | Path of the fake channel's shared state file (default `backend/.simulation/state.json`) |

## `frontend/.env.local`

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8001` | API base URL used by the browser. `VITE_*` values are public; never put secrets here |

## Per-channel settings (in the database)

These are changed in the web app, not in files:

| Setting | Where | Default |
| --- | --- | --- |
| Publishing mode | Settings → Publishing mode | Manual review |
| Reply preferences (tone, length, reply language, channel instructions) | Settings → Reply preferences | Friendly, Medium, English |
| Automatic discovery on/off, pause | Settings → Automatic operation, or the Automation page | Off |
| Allowed links, contacts and handles for replies | Settings → Automatic operation | Empty |
