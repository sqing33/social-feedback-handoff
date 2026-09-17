#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ENV="${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}"
if [ -f "$RUNTIME_ENV" ]; then
  set -a
  . "$RUNTIME_ENV"
  set +a
fi
DOMAIN="gui/$(id -u)"
HEADER=()
if [ -n "${SCRAPER_API_KEY:-}" ]; then
  HEADER=(-H "X-API-Key: $SCRAPER_API_KEY")
fi
if ! curl -fsS --max-time 8 "${HEADER[@]}" http://127.0.0.1:8007/api/status >/dev/null; then
  launchctl kickstart -k "$DOMAIN/com.mavis.social-feedback.scraper"
fi
PANEL_PORT="${PANEL_PORT:-5060}"
if ! curl -fsS --max-time 8 "http://127.0.0.1:${PANEL_PORT}/api/health" >/dev/null; then
  launchctl kickstart -k "$DOMAIN/com.mavis.social-feedback.panel"
fi
