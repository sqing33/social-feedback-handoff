#!/usr/bin/env python3
"""Create a consistent SQLite backup and retain the newest 14 copies."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "panel" / "social_feedback.db"
BACKUP_DIR = ROOT / ".runtime" / "backups"
RETENTION = 14


def main() -> None:
    if not SOURCE.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"social_feedback-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(SOURCE) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    target.chmod(0o600)
    backups = sorted(BACKUP_DIR.glob("social_feedback-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in backups[RETENTION:]:
        stale.unlink()
    print(target)


if __name__ == "__main__":
    main()
