#!/bin/bash
set -euo pipefail
if [ "$#" -ne 1 ]; then
  echo "usage: $0 {xhs|douyin|kuaishou|bilibili|twitter|youtube|tiktok}" >&2
  exit 2
fi
PLATFORM="$1"
case "$PLATFORM" in
  xhs|douyin|kuaishou|bilibili|twitter|youtube|tiktok) ;;
  *) echo "unsupported platform: $PLATFORM" >&2; exit 2 ;;
esac
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
curl -fsS "${HEADER[@]}" -X POST "http://127.0.0.1:8007/api/browser/login/$PLATFORM"
printf '\n'
