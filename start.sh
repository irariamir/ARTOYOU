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

# Report (not reveal) whether the Telegram bot is configured.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  BOT_STATE="enabled (from environment)"
elif [ -f .env ] && grep -qE '^TELEGRAM_BOT_TOKEN=.+' .env; then
  BOT_STATE="enabled (from .env)"
else
  BOT_STATE="disabled — set TELEGRAM_BOT_TOKEN in .env to enable"
fi

# First-user ownership: show whether the bot is already claimed.
if [ -f downloads/.bot_owner ]; then
  echo "🔐 Bot owner: $(cat downloads/.bot_owner) (change: rm downloads/.bot_owner)"
fi

echo "🤖 Telegram bot: ${BOT_STATE}"
echo "🚀 ARTOYOU is running on http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn server.main:app --host "${HOST}" --port "${PORT}"
