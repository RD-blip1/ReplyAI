# Installation

This guide takes a fresh clone of ReplyAI to a running local stack. Once it is installed,
continue with the [simulation guide](simulation-guide.md) (no credentials needed) or
[real-mode setup](real-mode-setup.md) (your channel and model providers).

> **Security rule:** never commit `backend/.env`, the Google OAuth client JSON, the Fernet
> key or any LLM API key. Only the `*.env.example` templates are tracked.

## 1. Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Git | recent | Cloning |
| Python | **3.12+** (`requires-python = ">=3.12"`) | Backend API and worker |
| Node.js + npm | **22+** | Frontend toolchain (Vite 8, React 19, TypeScript) |
| Docker Engine + Compose v2 | recent | PostgreSQL 17 and Redis 7 from `compose.yaml` |
| `bash`, `setsid`, `ss`, `curl` | standard on Linux | `scripts/dev.sh` |
| [uv](https://docs.astral.sh/uv/) | optional | Installing from the committed `backend/uv.lock` |

Linux, macOS and WSL 2 work for the manual steps. `scripts/dev.sh` relies on `setsid` and
`ss` from util-linux and iproute2, so on macOS start the three processes by hand
([see below](#by-hand)).

**No Docker?** Any local PostgreSQL 16/17 and Redis 7 work. Create a `replyai` role and
database that match `DATABASE_URL` in `backend/.env`. The role needs `CREATEDB` if you
want `scripts/dev.sh start sim` to create the `replyai_sim` database for you.

## 2. Clone and create the environment files

```bash
git clone https://github.com/RD-blip1/ReplyAI.git
cd ReplyAI

cp .env.example .env                          # Docker Compose: database credentials, host ports
cp backend/.env.example backend/.env          # API, worker and Alembic settings
cp frontend/.env.example frontend/.env.local  # VITE_API_BASE_URL
```

The defaults work as they are for simulation mode. Every variable is explained in
[configuration.md](configuration.md).

| File | Must match |
| --- | --- |
| `.env` (`POSTGRES_*`, `POSTGRES_PORT`, `REDIS_PORT`) | `DATABASE_URL` and `REDIS_URL` in `backend/.env` |
| `backend/.env` `CORS_ORIGINS`, `FRONTEND_BASE_URL` | The address you open the web app at |
| `frontend/.env.local` `VITE_API_BASE_URL` | The address the API listens on (default `http://127.0.0.1:8001`) |

## 3. Start PostgreSQL and Redis

```bash
docker compose up -d --wait postgres redis
docker compose ps          # both services should report "healthy"
```

Both services bind to `127.0.0.1` only. Data lives in the named volumes `postgres_data`
and `redis_data`: `docker compose down` keeps it, `docker compose down -v` deletes it.

## 4. Install the backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd ..
```

With uv, `cd backend && uv sync --extra dev` installs the exact locked versions into
`backend/.venv`. `scripts/dev.sh` expects the virtual environment at `backend/.venv`
either way.

The `dev` extra adds Ruff and the test tooling. A runtime-only install is
`pip install -e .` (or `uv sync --no-dev`).

## 5. Run the database migrations

```bash
cd backend
.venv/bin/alembic upgrade head
.venv/bin/alembic current        # expect: 20261026_03 (head)
cd ..
```

Migrations live in `backend/alembic/versions/` and are additive. Alembic reads
`DATABASE_URL` from `backend/.env`. Simulation mode uses a separate `replyai_sim`
database, which `scripts/dev.sh start sim` creates and migrates on demand.

## 6. Install the frontend

```bash
cd frontend
npm ci
cd ..
```

## 7. Start ReplyAI

### With the helper script (recommended)

```bash
scripts/dev.sh start sim     # fake channel + scripted models (default)
scripts/dev.sh start real    # your backend/.env: real Google OAuth and model providers
scripts/dev.sh status        # process state and the /ready report
scripts/dev.sh stop          # stop API, worker and web app
```

The script starts three background processes, each in its own process group, and
refuses to start if one is already running or if port 8001 or 5173 is taken:

| Process | Command | Port | Log |
| --- | --- | --- | --- |
| API | `uvicorn app.main:app` | 8001 | `logs/backend.log` |
| Worker | `python -m app.worker --worker-id dev-worker-<mode>` | – | `logs/worker.log` |
| Web app | `npm run dev` (Vite) | 5173 | `logs/frontend.log` |

In `sim` mode it also sets `REPLYAI_SIMULATION=1`, points `DATABASE_URL` at
`replyai_sim` and polls discovery every 30 seconds.

### By hand

Use three terminals from the repository root:

```bash
# Terminal 1: API
cd backend && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001

# Terminal 2: background worker (discovery + job queue)
cd backend && .venv/bin/python -m app.worker

# Terminal 3: web app
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173
```

Useful worker options: `--once` (process available work and exit), `--worker-id NAME`,
`--poll-interval SECONDS`, `--discovery-only`.

To run simulation mode by hand, create and migrate a database whose name ends in `_sim`
and start both the API and the worker with `REPLYAI_SIMULATION=1` and that
`DATABASE_URL`. See the [simulation guide](simulation-guide.md).

## 8. Health checks

```bash
curl -s http://127.0.0.1:8001/health
# {"status":"ok"}                                   the API process is alive

curl -s http://127.0.0.1:8001/ready
# {"status":"ready","checks":{"database":"available","redis":"available"}}
```

`/ready` returns HTTP 503 with `"not_ready"` and the failing check when PostgreSQL or
Redis is unreachable. Then open:

- Web app: <http://127.0.0.1:5173>
- Interactive API documentation (Swagger UI): <http://127.0.0.1:8001/docs>

## 9. Verify a development setup (optional)

```bash
cd backend && .venv/bin/ruff check . && cd ..
cd frontend && npx tsc -b --noEmit && npm run lint && npm run build && cd ..
```

## Next steps

- [Simulation guide](simulation-guide.md): explore every workflow without credentials.
- [Real-mode setup](real-mode-setup.md): Google Cloud, OAuth and model providers.
- [Troubleshooting](troubleshooting.md): if something above failed.
