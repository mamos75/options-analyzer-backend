#!/bin/bash
# Start Mamos Options Dashboard
set -e

cd "$(dirname "$0")/.."

export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:?Missing TELEGRAM_BOT_TOKEN}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:?Missing TELEGRAM_CHAT_ID}"

echo "▶ Mamos Crypto — Options Dashboard"
echo "  Backend : http://localhost:8000"
echo "  Frontend: open dashboard_options/frontend/index.html"
echo ""

# Active venv si présent
if [ -f "../venv/bin/activate" ]; then
  source ../venv/bin/activate
fi

# Install deps si absent
pip install -q -r backend/requirements.txt

# Start backend
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
