# Overnight Baseline Audit — Phase A

Recorded: 2026-10-18, autonomous session start. All statements below were verified against the
repository and a live local environment. Nothing is taken from historical claims.

## Repository state

- Root: `~/Projects/ReplyAI` (branch `main`, working tree **clean**, no untracked files).
- Recent commits: `06a240f` Merge PR #1 (release/replyai-v1), `e4b3713` docs, `52fe7bd` tests,
  `aa2bfa6` frontend, `27fb650` backend, `cb6d938` chore, `5ee13a7` init, `c4e4a2d` initial.
- Layout: `backend/` (FastAPI + SQLAlchemy async + Alembic + LangGraph), `frontend/`
  (React 19 + TypeScript + Vite), `compose.yaml` (PostgreSQL 17 + Redis 7), `docs/`.

## Environment verified during this session

- Docker Compose PostgreSQL 17 and Redis 7 healthy on 127.0.0.1:5432 / :6379 (pre-existing
  volumes preserved; never recreated).
- Backend already running: `uvicorn app.main:app --reload --port 8001` (user terminal, left
  running). `/health` 200, `/ready` 200 (PostgreSQL + Redis available).
- Frontend already running: Vite on 127.0.0.1:5173 (user terminal, left running). `npm run
  build` succeeds; `npm run lint` shows 3 pre-existing warnings, no errors.
- Alembic: `alembic current` = `850fa922f687 (head)`. All 14 migration files form one chain.
- Backend venv at `backend/.venv` with dev extras installed.

## Baseline test results (before any modification)

| Suite | Result |
| --- | --- |
| Backend `pytest` | **318 passed, 12 skipped**, 0 failed (73s) |
| Backend `ruff check .` | All checks passed |
| Frontend `vitest` | **70 passed** (8 files) |
| Frontend `npm run build` | Succeeded |
| Frontend `npm run lint` | 3 pre-existing warnings, no errors |

Skipped tests are opt-in live-provider tests (`RUN_LIVE_EVALS`, real OAuth/Groq), correctly
gated. The backend test harness points the whole application at per-test temporary SQLite
databases via `REPLYAI_TEST_DATABASE_URL` and refuses to construct an engine against the
development database during tests (`app/database.py`).

## Verified current architecture

- **Request flow**: React SPA → FastAPI (session cookie + CSRF) → SQLAlchemy async →
  PostgreSQL. Redis used for `/ready` and a video-context cache.
- **AI workflow**: `app/services/draft_workflow.py` (legacy sequential stages) or
  `app/services/draft_graph.py` (LangGraph), selected by `DRAFT_WORKFLOW_ENGINE`
  (default `legacy`). Stages: triage (secondary) → primary generation + independent
  secondary classification → secondary quality review → persist with revision-token guard.
- **LLM adapter** (`app/services/llm.py`): OpenAI-compatible chat completions, JSON-object
  mode, strict Pydantic schemas (`Triage`, `Classification`, `Generation`, `Quality`,
  `Refinement`), 5s connect/35s read timeout, exactly 2 attempts for transient errors,
  HTTP 429 → `rate_limited` (no retry), no tools/credentials in prompts, comment text
  delivered as JSON data separate from system instructions.
- **Publication** (`app/routers/publication.py`, `app/services/publishing.py`): the only
  mutating YouTube call is `comments.insert` (`insert_comment_reply`, `num_retries=0`,
  no token-refresh replay). Three duplicate-protection layers: (1) partial unique index
  allowing one open `reply_publications` row per draft; (2) atomic draft claim via
  `operation_token`/`publication_token`; (3) durable `published_reply_guards` unique on
  `(channel_id, parent_comment_id)` that survives draft deletion. Outcomes are strictly
  `posted` / `failed` (documented refusal only) / `unknown` (timeout, 5xx, no reply id,
  lost DB write) — unknown never retries automatically.
- **Publishing modes** (`publishing_settings`): `manual_review` (default), `dry_run`,
  `auto_on_agreement`, `approve_all`. Automatic modes apply only to a just-generated
  revision, require on-topic triage + classifier agreement (no uncertain/other) + all-pass
  quality review at confidence ≥ 0.80 + no findings, are rate-limited to 10 sends/hour via
  an atomic conditional UPDATE, and recheck mode/revision, scope, and connection immediately
  before send. Dry Run records a `draft_evaluations` row only and creates no publication,
  no guard, and never calls `comments.insert`.
- **Ownership**: durable `channel_owners` keyed by verified channel; drafts/preferences/
  settings hang off it; session expiry and disconnect retain work.
- **Usage accounting**: append-only `reply_draft_usage` ledger, one row per provider HTTP
  request, idempotent unique attempt key.

## Confirmed limitations (verified, not suspected)

1. **No background processing of any kind.** Generation, verification, and publication all
   run inside the HTTP request that triggers them. There is no job table, worker, queue,
   scheduler, backlog processing, or batch operation (`docs/AI_DRAFTS.md` states this
   explicitly; code confirms it). A 100-comment batch would require 100 sequential browser
   requests and would die with any disconnect.
2. **No crash recovery for in-flight work.** An interrupted request leaves drafts in
   `pending` state with an `operation_token` claim that only becomes reclaimable after
   6 minutes; there is no lease, reaper, or reconciliation for interrupted generations.
3. **Rate limiting exists only for automatic sends** (10/hour per channel). There are no
   shared budgets for Groq requests/tokens or YouTube API reads; an in-flight batch could
   exhaust provider quotas with no backpressure.
4. **Retries are adapter-local only** (2 attempts for transient provider errors). No
   classified job-level retries, no `Retry-After` handling, no backoff with jitter at the
   workflow level.
5. **`Retry-After` from Groq 429 is ignored** (mapped to `rate_limited`, header discarded).
6. **No multi-turn/thread context**: generation sees only the single comment; prior replies
   in the thread are not fetched or used.
7. **Language preference is advisory only**; nothing detects comment language or mismatch.
8. **Existing eval fixture is 10 examples** (`backend/tests/fixtures/eval_dataset.json`),
   used only by an opt-in live test (`test_triage_evals.py`, skipped by default).
9. **No worker observability**: no queue depth, worker liveness, or batch status endpoints.

## Behaviors that cannot be verified without real credentials

- Live Google OAuth consent, token refresh against Google, and real YouTube reads/writes.
- Live Groq generation quality (all provider tests use mocks; the live eval suite is
  opt-in and skipped).
- Real duplicate behavior at YouTube's side (documented as outside local guarantees).

## Suspected limitations (not fully confirmed this session)

- `reserve_automatic_slot`'s conditional-UPDATE window logic is exercised only through
  SQLite tests; PostgreSQL timezone edge behavior at window boundaries is untested here.
- Redis video-context cache has no invalidation beyond a 24h TTL (video title edits are
  invisible for up to a day). Low impact: triage uses it as supporting context only.

## Five highest-risk code paths

1. `insert_comment_reply` + `claim`/`settle` (`app/routers/publication.py`) — the only
   external mutation; duplicate replies and unknown outcomes originate here.
2. `publish_generated_draft` auto path — policy snapshot vs. send race; must never publish
   under a changed mode or after user edits.
3. `credentials_for_connection` (`app/services/youtube_api.py`) — token refresh and
   encryption; a bug here breaks all YouTube access or leaks tokens.
4. Draft mutation guards (`change`/`persist_result`) — stale-token checks are the only
   thing stopping an in-flight result from overwriting newer user work.
5. `get_owned_comment` ownership verification — a bypass would let a caller draft/publish
   against comments they do not own.

## Phase 2B gap assessment

The professor's target (automatic publication when both LLMs agree and the reply is safe;
human intervention only when needed; robust batch processing of ~100 comments) cannot be
met by the current synchronous request-scoped design. The guarded publication machinery
(claim/guard/unknown-outcome) is solid and is **reused unchanged** by the background
processing built in Phase C; the missing layer is durable job state, worker leases,
classified retries, shared rate budgets, pause-aware scheduling, and reconciliation.
