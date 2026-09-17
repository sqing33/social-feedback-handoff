#!/bin/bash
set -euo pipefail
DOMAIN="gui/$(id -u)"
TARGET_DIR="$HOME/Library/LaunchAgents"
for label in \
  com.mavis.social-feedback.backup \
  com.mavis.social-feedback.watchdog \
  com.mavis.social-feedback.panel \
  com.mavis.social-feedback.scraper; do
  launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
  launchctl disable "$DOMAIN/$label" >/dev/null 2>&1 || true
  plist="$TARGET_DIR/$label.plist"
  if [ -f "$plist" ]; then
    mv "$plist" "$HOME/.Trash/${label}.$(date +%Y%m%d%H%M%S).plist"
  fi
done
echo "launchd services stopped and disabled"
