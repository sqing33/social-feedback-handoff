#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${1:-${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}}"
RUNTIME_DIR="$(dirname "$ENV_FILE")"

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

if [ -f "$ENV_FILE" ]; then
  chmod 600 "$ENV_FILE"
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON_FALLBACK:-python3}"
fi
API_KEY="$($PYTHON_BIN - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"

umask 077
TMP_FILE="${ENV_FILE}.tmp.$$"
trap 'rm -f "$TMP_FILE"' EXIT
cat > "$TMP_FILE" <<EOF
FORMAL_MODE=1
ENABLE_LEGACY_API=0
ENABLE_BROWSER_ADMIN=0
ALLOW_LEGACY_WRITE_API=0
ALLOW_GENERIC_PUBLIC_CAPTURE=0
SCRAPER_API_KEY=$API_KEY
EOF
chmod 600 "$TMP_FILE"
mv "$TMP_FILE" "$ENV_FILE"
trap - EXIT
