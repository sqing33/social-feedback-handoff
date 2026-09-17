#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ENV="${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}"
if [ -f "$RUNTIME_ENV" ]; then
  set -a
  . "$RUNTIME_ENV"
  set +a
fi
HEADER=()
if [ -n "${SCRAPER_API_KEY:-}" ]; then
  HEADER=(-H "X-API-Key: $SCRAPER_API_KEY")
fi
printf '%s\n' 'Scraper:'
curl -fsS "${HEADER[@]}" http://127.0.0.1:8007/api/status
PANEL_PORT="${PANEL_PORT:-5060}"
printf '\n%s\n' 'Panel:'
curl -fsS "http://127.0.0.1:${PANEL_PORT}/api/health"
printf '\n%s\n' 'Launchd:'
for label in \
  com.mavis.social-feedback.scraper \
  com.mavis.social-feedback.panel \
  com.mavis.social-feedback.watchdog \
  com.mavis.social-feedback.backup; do
  launchctl print "gui/$(id -u)/$label" 2>/dev/null | sed -n '1,12p'
done
