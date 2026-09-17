#!/bin/bash
set -euo pipefail
DOMAIN="gui/$(id -u)"
launchctl kickstart -k "$DOMAIN/com.mavis.social-feedback.scraper"
launchctl kickstart -k "$DOMAIN/com.mavis.social-feedback.panel"
echo "launchd services restarted"
