# Troubleshooting

Start with the two quick checks, then find your symptom below.

```bash
scripts/dev.sh status                    # which processes run, plus the /ready report
curl -s http://127.0.0.1:8001/ready      # {"status":"ready","checks":{"database":...,"redis":...}}
```

Logs: `logs/backend.log`, `logs/worker.log` and `logs/frontend.log` (when started with
`scripts/dev.sh`), or the terminal of each process when started by hand.

## Docker is not running

| Symptom | Fix |
| --- | --- |
| `Cannot connect to the Docker daemon` | Start Docker Desktop or the Docker service (`sudo systemctl start docker`) |
| WSL 2: `docker: command not found` or no daemon | Enable **WSL integration** for your distro in Docker Desktop settings, or install Docker Engine inside the distro |
| `docker compose` is unknown | Install Compose v2 (`docker compose`, not the old `docker-compose`) |
| `--wait` times out | `docker compose ps` and `docker compose logs postgres redis`; a stale volume from another PostgreSQL major version fails to start (see below) |

No Docker available? Run PostgreSQL and Redis any other way and point `DATABASE_URL` and
`REDIS_URL` at them ([installation](installation.md#1-prerequisites)).

## Database connection errors

| Symptom | Fix |
| --- | --- |
| `/ready` shows `"database":"unavailable"` (HTTP 503) | `docker compose ps`; start with `docker compose up -d --wait postgres` |
| `password authentication failed for user "replyai"` | `POSTGRES_*` in `.env` and the credentials in `DATABASE_URL` differ. The volume keeps the password from its **first** start; either restore the old value or recreate it with `docker compose down -v` (deletes data) |
| `connection refused` on 5432 | A different port is configured: `POSTGRES_PORT` in `.env` must match the port in `DATABASE_URL` |
| `relation "…" does not exist` | Migrations not applied: `cd backend && .venv/bin/alembic upgrade head` |
| PostgreSQL container restarts with a data-directory version error | An old volume from another major version: `docker compose down -v`, then start again (deletes data) |
| `REPLYAI_SIMULATION requires a database whose name ends with '_sim'` | Simulation was started against the real database. Use `scripts/dev.sh start sim`, which points at `replyai_sim` |
| `REPLYAI_SIMULATION requires APP_ENVIRONMENT=development` | Set `APP_ENVIRONMENT=development` in `backend/.env` for simulation |
| `dev.sh start sim` fails while migrating | See `logs/backend.log`; the database role needs permission to create `replyai_sim` (the Compose user has it) |

## Redis unavailable

`/ready` reports `"redis":"unavailable"` and returns 503, but Redis is only a cache:
drafting and the queue keep working without it, just with more YouTube reads. Start it
with `docker compose up -d --wait redis` and make sure `REDIS_PORT` in `.env` matches
`REDIS_URL` in `backend/.env`.

## Google OAuth problems

| Symptom | Fix |
| --- | --- |
| "Google OAuth is not configured" in the web app | Set `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` to the **absolute** path of the client JSON and restart the API |
| `Error 400: redirect_uri_mismatch` | The authorized redirect URI on the Google client must equal `GOOGLE_REDIRECT_URI` exactly, including `127.0.0.1` vs `localhost`, the port and the path `/auth/google/callback` |
| `Error 403: access_denied` | Add your Google account as a **test user** on the OAuth consent screen |
| `encryption_key_invalid` | `TOKEN_ENCRYPTION_KEY` is not a valid Fernet key; generate one ([real-mode setup](real-mode-setup.md#2-backend-oauth-settings)) |
| Comments don't load or posting fails with `insufficient_scope` | Choose **Settings → Update YouTube permissions** to grant `youtube.force-ssl` |
| Jobs end as `needs_reauth` / "reconnect required" | The stored refresh token expired or was revoked (common while the consent screen is in Testing). Reconnect YouTube |
| After login the browser lands on the wrong address | `FRONTEND_BASE_URL` and `CORS_ORIGINS` must use the same host as the address you opened |

## Missing model-provider credentials

| Symptom | Fix |
| --- | --- |
| `ai_setup_required` / "AI setup required. Configure both models on the backend." | Set all six `PRIMARY_LLM_*` and `SECONDARY_LLM_*` values and restart the API **and** the worker |
| Provider HTTP 401/403, jobs end as `TECHNICAL_FAILURE` | Wrong API key or base URL. The base URL is the API prefix (e.g. `https://api.groq.com/openai/v1`), without `/chat/completions` |
| Every automatic decision is `two_llm_invariant_violated` | `PRIMARY_LLM_MODEL` and `SECONDARY_LLM_MODEL` are the same model; use two different models |
| Many `model_output_invalid` failures | The model doesn't follow JSON mode reliably; choose a model with JSON-mode support |

## Backend or frontend won't start

| Symptom | Fix |
| --- | --- |
| `ModuleNotFoundError` when starting the API | Dependencies not installed in `backend/.venv`: `.venv/bin/pip install -e '.[dev]'` |
| `scripts/dev.sh: … .venv/bin/python: No such file` | `dev.sh` expects the virtual environment at `backend/.venv` |
| `<name> failed to start; see logs/<name>.log` | Read the named log; it contains the real error |
| `a backend or worker started outside this script is running` | Stop the listed process, or run `scripts/dev.sh stop` if it was started by the script |
| Vite fails or `npm run dev` is unknown | Run `npm ci` in `frontend/`; check `node --version` is 22 or newer |
| Web app loads but every request fails (CORS or network error) | `VITE_API_BASE_URL` in `frontend/.env.local` must point at the API; its origin must be in `CORS_ORIGINS`. Restart Vite after changing `.env.local` |
| Python `SyntaxError` or dependency resolution errors | Python 3.12 or newer is required |

## Port conflicts

| Port | Used by | Change |
| --- | --- | --- |
| 5432 | PostgreSQL | `POSTGRES_PORT` in `.env` and the port in `DATABASE_URL` |
| 6379 | Redis | `REDIS_PORT` in `.env` and the port in `REDIS_URL` |
| 8001 | API | `dev.sh` uses 8001 fixed; when starting by hand, pick another `--port` and update `VITE_API_BASE_URL` and `GOOGLE_REDIRECT_URI` (and the Google client) |
| 5173 | Web app | `dev.sh` uses 5173 fixed; by hand, pass `--port` to Vite and update `CORS_ORIGINS` and `FRONTEND_BASE_URL` |

Find the process holding a port with `ss -ltnp 'sport = :8001'` (Linux) or
`lsof -i :8001` (macOS).

## Worker not processing jobs

Work through these in order:

1. **Is the worker running?** `scripts/dev.sh status`, or look for `python -m app.worker`.
2. **Does it start at all?** `invalid_lease_configuration` means
   `LLM_STAGE_TIMEOUT_SECONDS + 15` is not below `JOB_LEASE_SECONDS`; fix the values.
3. **Is automation enabled and not paused?** Check the **Automation** page. Discovery only
   runs for channels where it is on, and paused channels keep jobs waiting
   (`automation_paused`).
4. **Is discovery due?** Polls run at most every `DISCOVERY_POLL_SECONDS` (300 s by
   default, 30 s in simulation). Use **Poll now** on the Automation page.
5. **Are there new comments?** Discovery skips the channel's own comments, comments older
   than its cursor and comments that already have a draft.
6. **Is a budget exhausted?** Jobs in `retry` with `local_budget_exhausted`,
   `youtube_budget_exhausted` or `auto_publish_rate_limited` wait for the window to
   reset; see the next section.
7. **Is the per-channel cap reached?** At most `WORKER_MAX_JOBS_PER_CHANNEL` jobs per
   channel run at once (default 2).
8. **Is the worker using the same database as the API?** Both read `backend/.env`; in
   simulation both must get the `replyai_sim` URL (`dev.sh` does this).

`.venv/bin/python -m app.worker --once` processes available work once and exits, which
makes the problem visible in the terminal.

## API quota and rate-limit errors

| Symptom | Meaning | What to do |
| --- | --- | --- |
| `quota_exceeded` / "YouTube API quota is temporarily unavailable" | Google refused the request for quota reasons | Wait for the daily quota reset (Pacific Time), or request more quota in Google Cloud. Lower `YOUTUBE_UNITS_PER_DAY_BUDGET` so ReplyAI stops first |
| `youtube_budget_exhausted` | ReplyAI's own daily or per-minute YouTube budget is used up | Jobs retry later automatically. Raise the budget only if your Google quota allows it |
| Provider 429 / `rate_limited`, jobs in **Will retry** | The model provider throttled the request | Jobs retry with backoff. Lower `LLM_REQUESTS_PER_MINUTE` / `LLM_REQUESTS_PER_DAY` to stay under your plan |
| `local_budget_exhausted` | ReplyAI's own LLM request budget is used up | Waits for the next window; raise the budget only within your plan |
| `auto_publish_rate_limited` | The hourly or daily automatic-reply cap was reached | Intended; adjust `AUTO_PUBLISH_MAX_PER_HOUR` / `_PER_DAY` |
| Token meter at 100 % but the provider still works | The meter uses **your** configured ceilings, not the provider's live quota | Set `LLM_*_TOKENS_PER_DAY` to your plan's limits |

## Publication outcome is "unknown"

The reply request was sent but the response was lost, so ReplyAI doesn't know whether
YouTube created the reply. It never retries this automatically. Open the draft, choose
**Check YouTube** (a read-only search of the thread), and record whether it was posted.
