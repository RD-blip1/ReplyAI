#!/usr/bin/env bash
# Start/stop/inspect the local ReplyAI stack with stable logs and no duplicate processes.
#
#   scripts/dev.sh start [sim|real]   # default: sim
#   scripts/dev.sh stop
#   scripts/dev.sh status
#
# sim  : REPLYAI_SIMULATION=1, database 'replyai_sim' (created and migrated on demand), fake
#        YouTube channel, scripted models, discovery every 30 s. Nothing leaves this machine.
# real : database from backend/.env, real Google OAuth / YouTube / LLM provider. Publishing
#        mode in the database still decides whether anything posts (default manual_review).
#
# Logs: logs/{backend,worker,frontend}.log   PIDs: logs/*.pid   Mode: logs/mode
# PostgreSQL and Redis come from compose.yaml (docker compose up -d).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/logs"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
mkdir -p "$LOGS"

running() { [[ -f "$LOGS/$1.pid" ]] && kill -0 "$(cat "$LOGS/$1.pid")" 2>/dev/null; }

# Patterns identifying each service's real process (setsid may fork, so $! is unreliable).
pattern() {
  case "$1" in
    backend) echo '^[^ ]*python[^ ]* [^ ]*uvicorn app\.main:app --host 127\.0\.0\.1 --port 8001' ;;
    worker) echo '^[^ ]*python[^ ]* -m app\.worker --worker-id dev-worker-' ;;
    frontend) echo '^node [^ ]*vite --host 127\.0\.0\.1 --port 5173' ;;
  esac
}

port_busy() { ss -ltn "sport = :$1" | grep -q LISTEN; }

# Create the separate simulation database on first use and migrate it to the latest revision.
prepare_sim_database() {
  (cd "$BACKEND" && DATABASE_URL="$1" .venv/bin/python - <<'PY'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

url = make_url(os.environ["DATABASE_URL"])

async def main():
    engine = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"),
                                   {"name": url.database})
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
            print(f"created database {url.database}")
    await engine.dispose()

asyncio.run(main())
PY
  )
  (cd "$BACKEND" && DATABASE_URL="$1" .venv/bin/alembic upgrade head >> "$LOGS/backend.log" 2>&1) \
    || { echo "migrating the simulation database failed; see $LOGS/backend.log"; exit 1; }
}

start() {
  local mode="${1:-sim}"
  [[ "$mode" == sim || "$mode" == real ]] || { echo "mode must be sim or real"; exit 2; }
  for name in backend worker frontend; do
    if running "$name"; then echo "$name already running (pid $(cat "$LOGS/$name.pid")); run stop first"; exit 1; fi
  done
  local others='^[^ ]*python[^ ]* ([^ ]*uvicorn app\.main:app|-m app\.worker)'
  if pgrep -f "$others" >/dev/null; then
    echo "a backend or worker started outside this script is running; stop it first:"; pgrep -af "$others"; exit 1
  fi
  for port in 8001 5173; do port_busy "$port" && { echo "port $port is in use"; exit 1; }; done

  local env=()
  if [[ "$mode" == sim ]]; then
    local url
    url="$(cd "$BACKEND" && .venv/bin/python -c 'from sqlalchemy.engine import make_url; from app.config import settings; print(make_url(settings.database_url.get_secret_value()).set(database="replyai_sim").render_as_string(hide_password=False))')"
    prepare_sim_database "$url"
    env=(REPLYAI_SIMULATION=1 "DATABASE_URL=$url" DISCOVERY_POLL_SECONDS=30)
  fi
  echo "$mode" > "$LOGS/mode"

  (cd "$BACKEND" && setsid nohup env "${env[@]}" .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 \
      >> "$LOGS/backend.log" 2>&1 &)
  (cd "$BACKEND" && setsid nohup env "${env[@]}" PYTHONUNBUFFERED=1 .venv/bin/python -m app.worker --worker-id "dev-worker-$mode" \
      >> "$LOGS/worker.log" 2>&1 &)
  (cd "$FRONTEND" && setsid nohup npm run dev -- --host 127.0.0.1 --port 5173 --strictPort \
      >> "$LOGS/frontend.log" 2>&1 &)
  sleep 4
  for name in backend worker frontend; do
    pgrep -f "$(pattern "$name")" | head -1 > "$LOGS/$name.pid"
    running "$name" || { echo "$name failed to start; see $LOGS/$name.log"; exit 1; }
  done
  echo "started in $mode mode; logs in $LOGS"
}

stop() {
  for name in worker backend frontend; do
    if running "$name"; then
      # Each service runs in its own process group (setsid); stop the whole group.
      pgid="$(ps -o pgid= -p "$(cat "$LOGS/$name.pid")" | tr -d ' ')"
      kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$(cat "$LOGS/$name.pid")" 2>/dev/null || true
      echo "stopped $name"
    fi
    rm -f "$LOGS/$name.pid"
  done
}

status() {
  echo "mode: $(cat "$LOGS/mode" 2>/dev/null || echo unknown)"
  for name in backend worker frontend; do
    if running "$name"; then echo "$name: running (pid $(cat "$LOGS/$name.pid"))"; else echo "$name: stopped"; fi
  done
  curl -s -m 3 http://127.0.0.1:8001/ready || echo "backend not answering"; echo
}

case "${1:-}" in
  start) start "${2:-sim}" ;;
  stop) stop ;;
  status) status ;;
  *) echo "usage: $0 start [sim|real] | stop | status"; exit 2 ;;
esac
