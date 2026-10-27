# Development guide

This guide is for people changing ReplyAI. Read [architecture.md](architecture.md) first;
it explains the flows referred to here.

## Conventions

**Backend**

- Python 3.12, FastAPI, SQLAlchemy 2 (async), Pydantic v2. Lint with `ruff check .`
  (line length 100, configured in `pyproject.toml`).
- Routers handle HTTP concerns only (session, ownership, CSRF, status codes). Domain
  rules live in `app/services/`.
- Model outputs are validated by strict Pydantic schemas (`extra="forbid"`) in
  `services/llm.py`. Add new fields as optional so existing drafts stay valid.
- Concurrency control belongs in the database: conditional `UPDATE … WHERE` for claims
  and leases, unique indexes for invariants. Do not add in-process locks or caches that
  safety depends on.
- Logs use `event=<name> key=value` pairs. Never log tokens, API keys, OAuth codes, or
  full comment text.
- Errors returned to the browser are stable codes plus a human message (`routers/drafts.fail`);
  provider and Google error details stay on the server.

**Frontend**

- React 19 function components with hooks, TypeScript in strict mode, Tailwind CSS v4.
- All HTTP goes through `src/lib/api.ts` (`apiRequest`), which sends credentials and
  surfaces `ApiError`. Mutations send the session's `X-CSRF-Token`.
- API payload types live in `src/lib/types.ts`; keep them in step with the backend
  models.
- Lint with `npm run lint` (oxlint) and type-check with `npm run build`.

## Common commands

```bash
# Backend (from backend/)
.venv/bin/ruff check .
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
.venv/bin/python -m app.worker               # long-running worker
.venv/bin/python -m app.worker --once        # one job or one discovery pass, then exit
.venv/bin/python -m app.worker --discovery-only

# Frontend (from frontend/)
npm run dev -- --host 127.0.0.1
npm run lint
npm run build                                 # tsc -b && vite build

# Whole stack (from the repository root)
scripts/dev.sh start sim | start real | status | stop
```

## Extension points

| Change | Where | Notes |
| --- | --- | --- |
| Adjust a stage instruction | `services/prompts.py` | Every execution path reads the same instructions |
| Add a model output field | `services/llm.py` schema, then `services/prompts.py` | Make it optional; it must not join `LABEL_KEYS` unless you mean to change agreement |
| Change what a stage receives | `services/stage_inputs.py` | One contract for the UI, LangGraph and the worker |
| Add a decision rule | `services/automation_policy.decide()` | Insert it at the right priority; infrastructure checks must stay first |
| Add a reply-content rule | `services/reply_policy.py` | Return a `Violation` with a level; `review_required` and above block automatic publication |
| Add an input signal | `services/input_analysis.py` | Signals are observe-only unless a routing mode acts on them |
| Add a provider | Any OpenAI-compatible endpoint works through configuration | Other APIs need an adapter implementing `Provider` in `services/llm.py` |
| Add an API endpoint | `app/routers/`, registered in `app/main.py` | Use the `Session`/`Csrf`/`Db` dependencies and `owned()` for ownership |
| Add a page | `frontend/src/pages/`, route in `App.tsx`, nav item in `components/AppShell.tsx` | |

## API overview

Interactive OpenAPI documentation is served at `http://127.0.0.1:8001/docs`. The main
groups:

| Prefix | Purpose |
| --- | --- |
| `/health`, `/ready` | Liveness and readiness (PostgreSQL, Redis) |
| `/api/session`, `/auth/google/*`, `/auth/logout` | Session and Google OAuth |
| `/api/youtube/*` | Channel, videos, comments, posted replies (read-only) |
| `/api/drafts/*` | Create, regenerate, review, edit, refine and decide on drafts |
| `/api/drafts/{id}/publication` | Publish a confirmed draft; read its publication state |
| `/api/drafts/{id}/publication/check`, `/reconcile` | Resolve unknown publication outcomes |
| `/api/preferences`, `/api/publishing-settings` | Per-channel reply preferences and publishing mode |
| `/api/automation/*` | Discovery switches, activity, decision trace, allow-list |
| `/api/review-queue/*` | Drafts waiting for a person |
| `/api/batch/jobs/*` | Enqueue, list, cancel, requeue and inspect background jobs |
| `/api/simulation/*` | Simulation controls (only mounted when `REPLYAI_SIMULATION=1`) |

Every mutating endpoint requires the session cookie, an allowed `Origin` and the
`X-CSRF-Token` header. Draft mutations also carry the draft `revision` they were based
on; a stale revision is refused with `409`.

## Database migrations

- Migrations live in `backend/alembic/versions/`, named `YYYYMMDD_NN_description.py`
  with `revision = "YYYYMMDD_NN"`.
- Create one with `.venv/bin/alembic revision -m "describe change"`, then rename the file
  and set `revision`/`down_revision` to follow the convention.
- Keep migrations **additive** (new tables, new nullable columns, new indexes). Existing
  safety indexes (`published_reply_guards`, the one-open-attempt index, job claim
  indexes) must not be weakened.
- Try every migration on a disposable database first, for example:

  ```bash
  docker compose exec postgres createdb -U replyai replyai_migration_check
  DATABASE_URL=postgresql+asyncpg://replyai:replyai_dev_only@127.0.0.1:5432/replyai_migration_check \
    .venv/bin/alembic upgrade head
  docker compose exec postgres dropdb -U replyai replyai_migration_check
  ```

## Debugging the worker

- Run a single iteration in the foreground: `.venv/bin/python -m app.worker --once`.
- With `scripts/dev.sh`, follow `logs/worker.log` (`tail -f logs/worker.log`).
- The **Jobs** page shows every job with its status, attempts, last error and the
  decision recorded for it; **Automation** shows discovery status and recent decisions
  with a full decision trace.
- Useful queries (`docker compose exec postgres psql -U replyai replyai`):

  ```sql
  SELECT status, count(*) FROM reply_jobs GROUP BY status;
  SELECT id, status, attempts, recoveries, worker_id, lease_expires_at, last_error
    FROM reply_jobs WHERE status IN ('processing', 'verified', 'publish_claimed');
  SELECT decision_class, decision_code, created_at FROM automation_decisions
    ORDER BY created_at DESC LIMIT 20;
  SELECT key, window_started_at, window_count, day_started_at, day_count
    FROM rate_limit_counters ORDER BY updated_at DESC;
  ```

- A worker refuses to start if `LLM_STAGE_TIMEOUT_SECONDS + 15 >= JOB_LEASE_SECONDS`.

## Working safely on automatic publication

Automatic publication posts publicly in the creator's name. When you change anything on
that path:

1. Develop in **simulation mode** (`scripts/dev.sh start sim`). The publication path,
   policy, caps and guards are the real code; only YouTube and the models are fake.
2. On a real channel, use **Dry run** first: every decision is made and recorded, and
   nothing is sent.
3. Keep `routers/publication.py` ordering intact: checks and credential refresh, then
   revalidation, then claim (committed), then send, then settle. Never move the send
   before the claim commit.
4. Never add an automatic retry for an attempt whose outcome is `unknown`; resolve it
   with **Check YouTube** and a human decision.
5. Keep the hourly and daily caps and the per-comment publication guard. They are the
   last line of defence when every other check is wrong.

## Before opening a pull request

```bash
cd backend && .venv/bin/ruff check . && .venv/bin/python -c "import app.main, app.worker"
cd frontend && npm run lint && npm run build
```

Then exercise the changed flow in simulation mode and describe what you verified in the
pull request.
