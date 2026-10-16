# Fresh clone setup

This guide takes a brand-new clone of ReplyAI to a running local workspace. It assumes a
Linux, macOS, or WSL laptop with Docker. Nothing here requires your own data; every value
below is a development placeholder.

> Security rule: never commit `backend/.env`, the downloaded Google OAuth client JSON, the
> Fernet key, or any AI provider key. Only the `*.env.example` templates are tracked.

## 1. Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Git | any recent | |
| Python | 3.12+ | backend requires `>=3.12` |
| Node.js + npm | Node 22+ | frontend is Vite + React 19 |
| Docker Engine + Compose v2 | any recent | only for PostgreSQL and Redis |
| Google Cloud project | free | YouTube Data API v3 enabled |

On WSL, if you were just added to the `docker` group, open a new shell first. Do not loosen
Docker socket permissions.

## 2. Clone

```bash
git clone https://github.com/RD-blip1/ReplyAI.git
cd ReplyAI
```

If you forked it, use your fork's URL.

## 3. Create the environment files

```bash
cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
```

- `.env` holds only the local PostgreSQL credentials used by Compose.
- `backend/.env` is the real backend configuration (see the reference below).
- `frontend/.env.local` holds `VITE_API_BASE_URL`.

If you change `POSTGRES_PORT`/`REDIS_PORT` in the root `.env`, update the ports in
`backend/.env` `DATABASE_URL` and `REDIS_URL` to match.

## 4. Start PostgreSQL and Redis

```bash
docker compose up -d --wait postgres redis
docker compose ps
```

Both services should be `healthy`. Data lives in named volumes and survives restarts.
`docker compose down` keeps the volumes; only `docker compose down -v` deletes local data.

To run a second checkout alongside an existing one, give it unique host ports and a unique
project name:

```bash
POSTGRES_PORT=55432 REDIS_PORT=16379 docker compose -p replyai-alt up -d --wait postgres redis
```

## 5. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

`uv.lock` is committed; if you prefer `uv`:

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

`alembic upgrade head` is additive and creates every table on a fresh database. Restart the
API after editing `backend/.env`.

## 6. Frontend

In a second terminal:

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. The UI checks the API at `VITE_API_BASE_URL`
(default `http://127.0.0.1:8001`). API docs: <http://127.0.0.1:8001/docs>.

## 7. Google OAuth and the YouTube API

The app can start without this; the workspace just explains what is missing and `/health`,
`/ready`, and `/api/session` still work. To connect a channel:

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen (External is fine for testing) and add your Google
   account as a **test user**.
3. Create an **OAuth client ID → Web application**.
4. Add an **Authorized redirect URI** that matches `GOOGLE_REDIRECT_URI` exactly:
   `http://127.0.0.1:8001/auth/google/callback`.
5. Download the client JSON and keep it **outside** the repository:

   ```bash
   install -d -m 700 ~/.config/replyai
   mv ~/Downloads/client_secret_*.json ~/.config/replyai/google-oauth-client.json
   chmod 600 ~/.config/replyai/google-oauth-client.json
   realpath ~/.config/replyai/google-oauth-client.json
   ```

6. Put only the printed path into `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` in `backend/.env`.
7. Generate a Fernet key and put it in `TOKEN_ENCRYPTION_KEY`:

   ```bash
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

   Back it up securely. Losing it makes stored channel tokens unreadable.

Scopes: the initial **Connect YouTube** requests `youtube.readonly`. Reading and posting
comments requires the broader `youtube.force-ssl` grant, requested only through the explicit
**Update YouTube permissions** action after you review the disclosure. Declining leaves the
existing readonly connection intact.

## 8. AI draft provider (Groq)

Drafting is optional and stays disabled while the variables are blank. ReplyAI talks to any
OpenAI-compatible chat-completions endpoint, in two independent roles. For Groq:

1. Create an API key at <https://console.groq.com/keys>.
2. In `backend/.env` set:

   ```env
   PRIMARY_LLM_BASE_URL=https://api.groq.com/openai/v1
   PRIMARY_LLM_API_KEY=gsk_your_key_here
   PRIMARY_LLM_MODEL=openai/gpt-oss-120b
   SECONDARY_LLM_BASE_URL=https://api.groq.com/openai/v1
   SECONDARY_LLM_API_KEY=gsk_your_key_here
   SECONDARY_LLM_MODEL=openai/gpt-oss-20b
   ```

The adapter appends `/chat/completions` to the base URL. Both roles must be configured
explicitly. Restart the backend after editing.

## 9. Verify

```bash
# Backend (with .venv active, from backend/)
pytest            # offline; uses temporary SQLite, mocks providers and YouTube
ruff check .

# Frontend (from frontend/)
npm test
npm run build
npm run lint

# From the repository root
docker compose config --quiet
curl -i http://127.0.0.1:8001/health
curl -i http://127.0.0.1:8001/ready
curl -i http://127.0.0.1:8001/api/session
```

The backend suite needs **no** PostgreSQL or Redis: each test run creates a temporary SQLite
database and refuses to touch your configured database.

## Environment variable reference

### Root `.env` (Compose only)

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_DB` | `replyai` | database name |
| `POSTGRES_USER` | `replyai` | database user |
| `POSTGRES_PASSWORD` | `replyai_dev_only` | local-only password |
| `POSTGRES_PORT` | `5432` | host port mapping |
| `REDIS_PORT` | `6379` | host port mapping |

### `backend/.env`

| Variable | Required for | Notes |
| --- | --- | --- |
| `DATABASE_URL` | start | async SQLAlchemy URL (`postgresql+asyncpg://…`) |
| `REDIS_URL` | `/ready` | `redis://…` |
| `CORS_ORIGINS` | browser | allowed frontend origins |
| `FRONTEND_BASE_URL` | OAuth redirects | frontend origin |
| `APP_ENVIRONMENT` | production | `production` enables Secure cookies |
| `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` | YouTube connect | absolute path, outside the repo |
| `GOOGLE_REDIRECT_URI` | YouTube connect | must match Google client exactly |
| `TOKEN_ENCRYPTION_KEY` | YouTube connect | locally generated Fernet key |
| `DRAFT_WORKFLOW_ENGINE` | drafting | `legacy` or `langgraph` |
| `PRIMARY_LLM_BASE_URL` / `PRIMARY_LLM_API_KEY` / `PRIMARY_LLM_MODEL` | drafting | primary model role |
| `SECONDARY_LLM_BASE_URL` / `SECONDARY_LLM_API_KEY` / `SECONDARY_LLM_MODEL` | drafting | independent secondary role |

### `frontend/.env.local`

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_API_BASE_URL` | `http://127.0.0.1:8001` | backend base URL used by the browser |

## Troubleshooting

- **Port already in use (5432/6379/8001/5173).** Another checkout is running. Change the
  Compose host ports and `backend/.env`/`frontend/.env.local` to match, or stop the other
  stack.
- **`/ready` returns 503.** PostgreSQL or Redis is not up. Run `docker compose ps` and
  `docker compose logs postgres redis`.
- **OAuth `redirect_uri_mismatch`.** The Authorized redirect URI in Google Cloud must equal
  `GOOGLE_REDIRECT_URI` character-for-character (`http://127.0.0.1:8001/auth/google/callback`
  by default).
- **`encryption_key_invalid`.** `TOKEN_ENCRYPTION_KEY` is not a valid Fernet key; regenerate
  it with the command above.
- **AI drafting disabled.** Both LLM roles must be fully set, then restart the backend.
