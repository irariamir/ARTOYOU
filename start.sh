#!/usr/bin/env bash
# ARTOYOU — one-command launcher
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"

if [ ! -d .venv ]; then
  echo "🐍 Creating virtualenv…"
  python3 -m venv .venv
fi

if [ ! -f .venv/.deps-installed ] || [ server/requirements.txt -nt .venv/.deps-installed ]; then
  echo "📦 Installing dependencies…"
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r server/requirements.txt
  touch .venv/.deps-installed
fi

echo "🚀 ARTOYOU is running on http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn server.main:app --host "${HOST}" --port "${PORT}"
