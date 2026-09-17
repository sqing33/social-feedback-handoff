"""Runtime-only pairing and extension preparation for the safe Bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("scraper.xhs_bridge.runtime")

_ENABLED_VALUES = {"1", "true", "yes", "on"}
_runtime_token: str | None = None


def enabled() -> bool:
    return (os.environ.get("XHS_BRIDGE_ENABLED", "0") or "").strip().lower() in _ENABLED_VALUES


def token() -> str:
    """Return an in-process pairing token without persisting or logging it."""
    global _runtime_token
    if _runtime_token is None:
        configured = os.environ.get("XHS_BRIDGE_TOKEN", "").strip()
        api_key = os.environ.get("SCRAPER_API_KEY", "").strip()
        if configured:
            _runtime_token = configured
        elif api_key:
            _runtime_token = hmac.new(
                api_key.encode("utf-8"),
                b"mavis-xhs-safe-bridge-v1",
                hashlib.sha256,
            ).hexdigest()
        else:
            _runtime_token = secrets.token_urlsafe(32)
    return _runtime_token


def extension_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return Path(
        os.environ.get(
            "XHS_BRIDGE_EXTENSION_RUNTIME_DIR",
            str(root / "data" / "xhs-safe-extension"),
        )
    ).resolve()


def prepare_extension() -> str:
    """Create a local runtime copy with the process pairing token.

    The source extension contains no token. The generated copy lives below the
    ignored runtime data directory and is only loaded when the opt-in flag is
    enabled. The token is never logged or returned by an API.
    """
    if not enabled():
        return ""
    source = Path(__file__).resolve().parents[2] / "xhs-safe-extension"
    static_assets = ("manifest.json", "popup.html", "popup.js")
    required_assets = (*static_assets, "background.js")
    if any(not (source / name).is_file() for name in required_assets):
        logger.warning("safe XHS extension source is missing")
        return ""
    target = extension_path()
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    for name in static_assets:
        (target / name).write_text(
            (source / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        os.chmod(target / name, 0o600)
    background = (source / "background.js").read_text(encoding="utf-8")
    background = background.replace(
        'return "__XHS_BRIDGE_TOKEN__";',
        f"return {json.dumps(token())};",
        1,
    )
    (target / "background.js").write_text(background, encoding="utf-8")
    os.chmod(target / "background.js", 0o600)
    return str(target)


def reset_for_tests() -> None:
    global _runtime_token
    _runtime_token = None
