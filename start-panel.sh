#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_ENV="${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}"
"$ROOT/scripts/ensure-runtime-env.sh" "$RUNTIME_ENV"
set -a
. "$RUNTIME_ENV"
set +a
cd "$ROOT/panel"
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
export SCRAPER_URL="${SCRAPER_URL:-http://127.0.0.1:8007}"
export SCRAPER_TIMEOUT_SECONDS="${SCRAPER_TIMEOUT_SECONDS:-75}"
export FORMAL_MODE="${FORMAL_MODE:-1}"
export ALLOW_LEGACY_WRITE_API="${ALLOW_LEGACY_WRITE_API:-0}"
export PANEL_PORT="${PANEL_PORT:-5060}"
exec "$PYTHON_BIN" -m waitress --listen=127.0.0.1:${PANEL_PORT} --threads="${PANEL_THREADS:-6}" wsgi:app
