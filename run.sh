#!/usr/bin/env bash
# Local launcher: starts the dashboard + in-process agent worker on one command.
# Open http://127.0.0.1:8765 after it boots.
set -euo pipefail
cd "$(dirname "$0")"

# "Ask the Bot" (local MLX chat) needs mlx-lm + sentence-transformers, which
# live in an optional venv (see README). If that venv exists, run the server
# with it so the feature is live; otherwise plain python3 — the dashboard
# works identically, with the chat tab gracefully disabled.
PYTHON="${TRADEBOT_PYTHON:-$HOME/.polymarket-paper/venv/bin/python3}"
[ -x "$PYTHON" ] || PYTHON=python3

"$PYTHON" -m pip install -q -r requirements.txt 2>/dev/null || true

# Load local secrets/overrides if present (.env is gitignored — e.g. the
# dashboard login TRADEBOT_AUTH_USER / TRADEBOT_AUTH_PASSWORD).
if [ -f "$(dirname "$0")/.env" ]; then
  set -a; . "$(dirname "$0")/.env"; set +a
fi

export TRADEBOT_STANDALONE=1
export TRADEBOT_START_BALANCE="${TRADEBOT_START_BALANCE:-200}"
export PORT="${PORT:-8765}"
# Local DB path (omit to use ~/.polymarket-paper/portfolio.db)
# export TRADEBOT_DB_PATH="$PWD/portfolio.db"

echo "Starting TradeBOT on http://127.0.0.1:${PORT} ($PYTHON) ..."
exec "$PYTHON" backend/server.py
