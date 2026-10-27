# Getting started

This guide takes a fresh clone of ReplyAI to a running local workspace. It covers two
ways to run the app:

- **Simulation mode**: a fake YouTube channel and scripted models. No Google account, no
  API keys, nothing leaves your machine. Start here.
- **Real mode**: your own channel through Google OAuth and real LLM providers.

> **Security rule:** never commit `backend/.env`, the downloaded Google OAuth client JSON,
> the Fernet key, or any LLM API key. Only the `*.env.example` templates are tracked.

## 1. Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Git | recent | |
| Python | 3.12+ | backend requires `>=3.12` |
| Node.js + npm | 22+ | frontend toolchain (Vite 8, React 19) |
| Docker Engine + Compose v2 | recent | runs PostgreSQL 17 and Redis 7 |

Linux, macOS and WSL 2 are supported. `scripts/dev.sh` uses `bash`, `setsid` and `ss`,
which are available on Linux and WSL.

## 2. Clone and create environment files

```bash
git clone https://github.com/RD-blip1/ReplyAI.git
cd ReplyAI

cp .env.example .env                       # PostgreSQL credentials for Docker Compose
cp backend/.env.example backend/.env       # backend configuration
cp frontend/.env.example frontend/.env.local
```

The defaults work as-is for simulation mode. See [configuration.md](configuration.md)
for every variable.

## 3. Start PostgreSQL and Redis

```bash
docker compose up -d --wait postgres redis
docker compose ps        # both services should be "healthy"
```

Data lives in named Docker volumes and survives restarts. `docker compose down` keeps
the data; `docker compose down -v` deletes it.

## 4. Install dependencies and migrate

```bash
# Backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
cd ..

# Frontend
cd frontend
npm ci
cd ..
```

`backend/uv.lock` is committed; with [uv](https://docs.astral.sh/uv/) you can use
`uv sync --extra dev` instead of `pip`.

## 5a. Run in simulation mode (recommended first run)

```bash
scripts/dev.sh start sim
```

The script creates and migrates a separate `replyai_sim` database, then starts the API
(port 8001), the worker and the web app (port 5173) in the background with
`REPLYAI_SIMULATION=1`. Open <http://127.0.0.1:5173>.

1. Click **Connect simulated channel**.
2. Browse **Videos** and the **Comment inbox**, generate a draft, and inspect the
   verification results in the draft panel.
3. On **Automation**, enable discovery and add comments with the simulation panel. Each
   scenario (safe, model disagreement, spam, PII, URL, sarcasm, Hindi, Hinglish, hidden
   characters, rate limit, …) exercises a different branch of the decision policy.
4. Switch **Settings → Publishing mode** to *Dry run* or *Auto-publish on agreement* and
   watch the **Review queue** and the **Posted** history fill in.

Simulation refuses to run unless `APP_ENVIRONMENT=development` and the database name
ends in `_sim`, so it can never write simulated records into your real database.

```bash
scripts/dev.sh status    # process state and /ready
scripts/dev.sh stop      # stop API, worker and web app
```

Logs are written to `logs/backend.log`, `logs/worker.log` and `logs/frontend.log`.

## 5b. Run in real mode

Real mode needs a Google OAuth client and, for drafting, LLM credentials.

### Google OAuth and the YouTube Data API

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project and
   enable **YouTube Data API v3**.
2. Configure the OAuth consent screen (External is fine for testing) and add your Google
   account as a **test user**.
3. Create an **OAuth client ID → Web application** with the authorized redirect URI
   `http://127.0.0.1:8001/auth/google/callback` (it must equal `GOOGLE_REDIRECT_URI`
   exactly).
4. Download the client JSON and keep it **outside** the repository:

   ```bash
   install -d -m 700 ~/.config/replyai
   mv ~/Downloads/client_secret_*.json ~/.config/replyai/google-oauth-client.json
   chmod 600 ~/.config/replyai/google-oauth-client.json
   realpath ~/.config/replyai/google-oauth-client.json
   ```

5. Put the printed path into `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` in `backend/.env`.
6. Generate a Fernet key for `TOKEN_ENCRYPTION_KEY` and back it up securely (losing it
   makes stored channel tokens unreadable):

   ```bash
   backend/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

**Connect YouTube** first requests read-only access. Reading comment threads and posting
replies needs the broader `youtube.force-ssl` scope, requested only when you choose
**Update YouTube permissions**.

### LLM providers

ReplyAI works with any OpenAI-compatible chat-completions endpoint. For
[Groq](https://console.groq.com/keys), set in `backend/.env`:

```env
PRIMARY_LLM_BASE_URL=https://api.groq.com/openai/v1
PRIMARY_LLM_API_KEY=<your key>
PRIMARY_LLM_MODEL=openai/gpt-oss-120b
SECONDARY_LLM_BASE_URL=https://api.groq.com/openai/v1
SECONDARY_LLM_API_KEY=<your key>
SECONDARY_LLM_MODEL=openai/gpt-oss-20b
```

Drafting stays disabled until both roles are configured. The primary and secondary
models must differ: automatic publication refuses a draft whose generator and verifier
are the same model.

### Start

```bash
scripts/dev.sh start real
```

or start each process in its own terminal:

```bash
cd backend && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
cd backend && .venv/bin/python -m app.worker
cd frontend && npm run dev -- --host 127.0.0.1 --port 5173
```

The publishing mode starts as **Manual review**: nothing is posted until you confirm a
reply, or explicitly choose an automatic mode in Settings. Try **Dry run** before
**Auto-publish on agreement**.

## 6. Check that everything is up

```bash
curl -s http://127.0.0.1:8001/health      # process is alive
curl -s http://127.0.0.1:8001/ready       # PostgreSQL and Redis reachable
```

Interactive API documentation is served at <http://127.0.0.1:8001/docs>.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Port 5432, 6379, 8001 or 5173 already in use | Stop the other process, or change `POSTGRES_PORT` / `REDIS_PORT` in `.env` and the matching URLs in `backend/.env` |
| `/ready` returns 503 | PostgreSQL or Redis is down: `docker compose ps`, `docker compose logs postgres redis` |
| `redirect_uri_mismatch` from Google | The redirect URI in Google Cloud must equal `GOOGLE_REDIRECT_URI` character for character |
| `encryption_key_invalid` | `TOKEN_ENCRYPTION_KEY` is not a valid Fernet key; generate a new one |
| "AI drafting is not configured" | Set all six `PRIMARY_LLM_*` / `SECONDARY_LLM_*` values and restart the API |
| `scripts/dev.sh` says a backend is already running | Run `scripts/dev.sh stop`, or stop the process it lists |
| Worker exits with `invalid_lease_configuration` | `LLM_STAGE_TIMEOUT_SECONDS + 15` must be less than `JOB_LEASE_SECONDS` |
