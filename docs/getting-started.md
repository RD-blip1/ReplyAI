# Getting started

The shortest path from a fresh clone to a running ReplyAI. For explanations of each step,
see [installation.md](installation.md).

**Needs:** Git, Python 3.12+, Node.js 22+, Docker with Compose v2.

```bash
git clone https://github.com/RD-blip1/ReplyAI.git
cd ReplyAI

cp .env.example .env
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local

docker compose up -d --wait postgres redis

cd backend && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' \
  && .venv/bin/alembic upgrade head && cd ..
cd frontend && npm ci && cd ..

scripts/dev.sh start sim
```

Open <http://127.0.0.1:5173> → **Connect simulated channel**.

```bash
scripts/dev.sh status     # processes and /ready
scripts/dev.sh stop
```

## Where next

| Goal | Guide |
| --- | --- |
| Explore the app with the fake channel | [simulation-guide.md](simulation-guide.md) |
| Connect your channel and real models | [real-mode-setup.md](real-mode-setup.md) |
| Configure Google OAuth (the web app says "Google OAuth is not configured") | [real-mode-setup.md](real-mode-setup.md#1-google-cloud-project-and-youtube-data-api) |
| Something didn't start | [troubleshooting.md](troubleshooting.md) |
| Change the code | [development.md](development.md) |

> **Security rule:** never commit `backend/.env`, the Google OAuth client JSON, the Fernet
> key or any API key. Only the `*.env.example` templates are tracked.
