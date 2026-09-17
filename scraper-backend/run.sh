#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ENV="${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}"
if [ -f "$RUNTIME_ENV" ]; then
  set -a
  . "$RUNTIME_ENV"
  set +a
fi
cd "$ROOT/scraper-backend"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON_FALLBACK:-python3}"
fi
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(
        f"Python 3.10+ is required (found {sys.version.split()[0]}). "
        "Create .venv with Python 3.11 or set PYTHON_BIN."
    )
PY
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8007}"
export FORMAL_MODE="${FORMAL_MODE:-1}"
export ENABLE_LEGACY_API="${ENABLE_LEGACY_API:-0}"
export ENABLE_BROWSER_ADMIN="${ENABLE_BROWSER_ADMIN:-0}"
export BROWSER_USER_DATA_DIR="${BROWSER_USER_DATA_DIR:-$HOME/.minimax/social-feedback/browser}"
export BROWSER_HEADLESS="${BROWSER_HEADLESS:-false}"
if [ -z "${BROWSER_EXECUTABLE_PATH:-}" ] && [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  export BROWSER_EXECUTABLE_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
fi
exec "$PYTHON_BIN" -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
