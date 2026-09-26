# ReplyAI Project Documentation

## Purpose

ReplyAI is planned as a YouTube comment-management assistant. The eventual workflow may connect a channel, retrieve comments, use independent language-model checks to prepare replies, and require human review before any controlled posting. No external account, comment, model, or publishing integration is implemented in Phase 1.

## Phase 1 Architecture

- `frontend/`: React, TypeScript, Vite, and Tailwind CSS. The local status screen calls the backend health endpoints.
- `backend/`: FastAPI application, Pydantic settings, SQLAlchemy async PostgreSQL connectivity, Alembic migration scaffolding, and Redis connectivity.
- `compose.yaml`: PostgreSQL and Redis only. The frontend and API run directly in WSL.
- PostgreSQL and Redis ports bind to `127.0.0.1`; named volumes retain their data across container restarts.
- Backend `/health` reports application liveness. `/ready` performs `SELECT 1` and a Redis `PING`, reports only availability labels, and returns HTTP 503 if either check fails.

## Local Setup

Requirements: Python 3.12+, Node.js 22+, npm, Docker Engine, and the Docker Compose plugin. The current WSL account must have Docker access; after adding it to the `docker` group, open a new WSL session before using Docker directly.

From the repository root:

```bash
cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
docker compose up -d postgres redis
```

Create the Python environment and install the backend plus development dependencies:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

In another WSL terminal, install and run the frontend:

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open <http://localhost:5173>. The browser API base is configured by `frontend/.env.local`; its development default is `http://127.0.0.1:8001`. Restart Vite after changing that value.

## Verification

Run backend checks from `backend/` with its virtual environment active:

```bash
pytest
ruff check .
```

Run frontend checks from `frontend/`:

```bash
npm run build
npm run lint
```

Check Compose configuration from the repository root with `docker compose config --quiet`. Once the containers are running, `docker compose ps` should show PostgreSQL and Redis as healthy, and `curl -i http://127.0.0.1:8001/ready` should return HTTP 200 with both dependencies available. `/health` should return HTTP 200 independently of database or Redis state.

### Phase 1 Results

- Frontend production build and lint passed.
- Backend tests passed (3 tests); Ruff passed.
- Compose configuration validated; PostgreSQL and Redis reported healthy.
- Live `/health` and `/ready` requests returned HTTP 200; readiness reported both dependencies available.
- Browser check confirmed all three local-service indicators available, later-phase features unavailable, and no horizontal overflow at a 320px viewport.
- Port 8000 was already occupied by another local process, so this checkout uses API port 8001. That process was left untouched.
- Alembic's online CLI configuration loaded against the local PostgreSQL service; no migration revision was applied.

## Environment and Data Safety

The committed `.env.example` files contain local-only development placeholders, not production credentials. Actual `.env` files are ignored. Change the backend PostgreSQL URL when changing the Compose `POSTGRES_*` values. Do not commit secrets or reuse the development password. Compose volumes are named `replyai_postgres_data` and `replyai_redis_data`; stopping services does not delete them. Do not use `docker compose down -v` unless intentionally deleting local data.

## Not Yet Implemented

- Google OAuth 2.0 or YouTube Data API access
- Channel discovery, comment fetching, or comment persistence
- LLM providers, dual-model verification, or LangGraph workflows
- Reply drafting, approval queues, automatic posting, or publishing controls
- Domain-specific migration revisions or application database tables

## Next Steps

1. Design the persistence model and create the first Alembic migration.
2. Define secure Google OAuth and YouTube API boundaries, scopes, and token storage.
3. Define provider-agnostic LLM interfaces and verification behavior before adding reply workflows.
4. Add comment review and approval flows before considering controlled publishing.