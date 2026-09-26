# ReplyAI

ReplyAI is planned as a YouTube comment-management assistant. This repository currently contains **Phase 1: the local development foundation**. It provides a React + TypeScript frontend, a FastAPI backend, and local PostgreSQL/Redis services. YouTube, OAuth, AI, and publishing workflows are not implemented; the UI shows no mock channel or comment data.

## Requirements

- Python 3.12 or newer
- Node.js 22 or newer and npm
- Docker Engine and Docker Compose
- WSL with Docker access for local infrastructure

If your account was just added to the `docker` group, start a new WSL session before running Compose. Do not change Docker socket permissions.

## Start Locally

From the repository root (`~/Projects/ReplyAI`):

```bash
cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
docker compose up -d --wait postgres redis
```

The environment templates use development-only placeholders. If you change `POSTGRES_*` in `.env`, update the matching credentials in `backend/.env`. Never commit actual `.env` files or use the example password outside local development.

In a WSL terminal, start the API directly (not in Docker):

```bash
cd ~/Projects/ReplyAI/backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

In a second WSL terminal, start the frontend directly:

```bash
cd ~/Projects/ReplyAI/frontend
npm install
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. The page checks the API at `http://127.0.0.1:8001`; API documentation is at <http://127.0.0.1:8001/docs>. Change the frontend URL in `frontend/.env.local` if you run the API on a different port, then restart Vite.

## Verify

```bash
# From backend/ with .venv active
pytest
ruff check .

# From frontend/
npm run build
npm run lint

# From the repository root
docker compose config --quiet
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8001/ready
```

`/health` reports application liveness independently of dependencies. `/ready` checks PostgreSQL and Redis and returns HTTP 503 if either is unavailable. It reports only availability labels, never connection details.

### Verification Results

- Frontend production build and lint passed; npm reported zero vulnerabilities during dependency installation.
- Backend tests passed (3 tests) and Ruff passed. Pytest emitted one upstream Starlette deprecation warning about its `httpx` test client.
- Alembic CLI configuration loaded against the local database; no migration revision has been applied.
- Compose configuration validated, and PostgreSQL/Redis both reached `healthy`.
- Live `/health` and `/ready` requests returned HTTP 200; readiness reported both dependencies available.
- Browser checks showed all three local services available and all four later-phase features unavailable, with no horizontal overflow at 320px width.
- Port 8000 was occupied by another local Python process, which was left untouched. ReplyAI uses port 8001.

## Implemented

- Responsive React + TypeScript status page with Tailwind CSS and live API/dependency checks.
- FastAPI `/health` and `/ready` endpoints with credential-safe responses, SQLAlchemy async connectivity, and Alembic migration scaffolding.
- Environment-based backend configuration and example environment files.
- Docker Compose PostgreSQL and Redis services, localhost-bound ports, health checks, and persistent named volumes.
- Focused backend health/readiness tests.

## Not Implemented Yet

Google OAuth 2.0, YouTube Data API calls, channel/comment data, LLM providers, LangGraph, domain tables and migration revisions, reply generation, human approval workflows, and posting are all deferred. No API keys are needed for this phase.

## Data Safety

PostgreSQL and Redis data are stored in named Compose volumes and survive container restarts. `docker compose down` stops/removes containers but keeps those volumes. Do not use `docker compose down -v` unless you intentionally want to delete local database/cache data.

## Next Steps

1. Define application data models and create the first Alembic migration.
2. Design Google OAuth, YouTube API scopes, and secure token storage.
3. Define a provider-agnostic LLM interface and independent verification boundary.
4. Implement comment review and human approval before any controlled posting.

See [docs/PROJECT_DOCUMENTATION.md](docs/PROJECT_DOCUMENTATION.md) for architecture and [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) for phase implementation and verification results.