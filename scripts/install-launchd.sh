#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$ROOT/.runtime/logs"
"$ROOT/scripts/ensure-runtime-env.sh" "${FORMAL_ENV_FILE:-$ROOT/.runtime/formal.env}"
mkdir -p "$TARGET_DIR" "$LOG_DIR" "$ROOT/.runtime/backups"
chmod 700 "$ROOT/.runtime" "$LOG_DIR" "$ROOT/.runtime/backups"

ROOT="$ROOT" TARGET_DIR="$TARGET_DIR" LOG_DIR="$LOG_DIR" python3 - <<'PY'
import os
import plistlib
from pathlib import Path

root = Path(os.environ["ROOT"])
target = Path(os.environ["TARGET_DIR"])
logs = Path(os.environ["LOG_DIR"])
path_value = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
services = [
    (
        "com.mavis.social-feedback.scraper",
        ["/bin/bash", str(root / "start-scraper.sh")],
        {"RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10, "ProcessType": "Interactive"},
    ),
    (
        "com.mavis.social-feedback.panel",
        ["/bin/bash", str(root / "start-panel.sh")],
        {"RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10, "ProcessType": "Background"},
    ),
    (
        "com.mavis.social-feedback.watchdog",
        ["/bin/bash", str(root / "scripts" / "watchdog.sh")],
        {"RunAtLoad": True, "StartInterval": 60, "ProcessType": "Background"},
    ),
    (
        "com.mavis.social-feedback.backup",
        [str(root / ".venv" / "bin" / "python"), str(root / "scripts" / "backup_db.py")],
        {"RunAtLoad": True, "StartCalendarInterval": {"Hour": 3, "Minute": 15}, "ProcessType": "Background"},
    ),
]
for label, arguments, schedule in services:
    short = label.rsplit(".", 1)[-1]
    payload = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"PATH": path_value},
        "StandardOutPath": str(logs / f"{short}.out.log"),
        "StandardErrorPath": str(logs / f"{short}.err.log"),
        **schedule,
    }
    output = target / f"{label}.plist"
    with output.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    output.chmod(0o600)
    print(output)
PY

DOMAIN="gui/$(id -u)"
LABELS=(
  com.mavis.social-feedback.scraper
  com.mavis.social-feedback.panel
  com.mavis.social-feedback.watchdog
  com.mavis.social-feedback.backup
)
for label in "${LABELS[@]}"; do
  launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
done
sleep 1
for label in "${LABELS[@]}"; do
  plist="$TARGET_DIR/$label.plist"
  launchctl bootstrap "$DOMAIN" "$plist"
  launchctl enable "$DOMAIN/$label"
done

echo "launchd services, watchdog, and daily backup installed"
