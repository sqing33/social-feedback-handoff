#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ENV="${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}"
if [ -f "$RUNTIME_ENV" ]; then
  set -a
  . "$RUNTIME_ENV"
  set +a
fi
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/real_probe.py" "$@"
