"""Safe Bridge protocol definitions.

The protocol deliberately exposes only fixed, read-oriented commands. The
browser extension owns the page and returns already-filtered public data.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

SAFE_METHODS = frozenset({
    "ping_server",
    "get_runtime_status",
    "get_xhs_page_state",
    "click_xhs_note_card",
})

_ALLOWED_HOSTS = frozenset({"xiaohongshu.com", "www.xiaohongshu.com"})
_ALLOWED_PARAMS = {
    "ping_server": frozenset(),
    "get_runtime_status": frozenset(),
    "get_xhs_page_state": frozenset({"note_id"}),
    "click_xhs_note_card": frozenset({"note_id"}),
}
_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_params(method: str, params: Any) -> dict[str, Any]:
    """Validate the tiny parameter surface and reject credential-shaped keys."""
    validate_method(method)
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("Bridge params must be an object")
    unexpected = set(params) - _ALLOWED_PARAMS[method]
    if unexpected:
        raise ValueError("Bridge params contain unsupported fields")
    result = dict(params)
    if "note_id" in result:
        note_id = str(result["note_id"] or "")
        if not _NOTE_ID_RE.fullmatch(note_id):
            raise ValueError("Bridge note_id is invalid")
        result["note_id"] = note_id
    return result


def sanitize_result(method: str, value: Any) -> Any:
    """Allow only the documented public result shape across the process boundary."""
    validate_method(method)
    if method == "ping_server":
        if not isinstance(value, dict):
            return {}
        return {
            "extension_connected": bool(value.get("extension_connected")),
            "generation": int(value.get("generation") or 0),
            "allowed_methods": sorted(SAFE_METHODS),
        }
    if method == "get_runtime_status":
        if not isinstance(value, dict):
            return {}
        path = str(value.get("url_path") or "/")
        if not path.startswith("/") or any(char in path for char in "?#"):
            path = "/"
        return {"tab_present": bool(value.get("tab_present")), "url_path": path[:512]}
    if method == "click_xhs_note_card":
        return bool(value)
    if not isinstance(value, dict):
        return {}
    allowed = {
        "path", "target_match", "detail_ready", "login_wall",
        "verification_wall", "inaccessible",
    }
    if set(value) - allowed:
        raise ValueError("Bridge result contains unsupported fields")
    path = str(value.get("path") or "/")
    if not path.startswith("/") or any(char in path for char in "?#"):
        path = "/"
    return {
        "path": path[:512],
        "target_match": bool(value.get("target_match")),
        "detail_ready": bool(value.get("detail_ready")),
        "login_wall": bool(value.get("login_wall")),
        "verification_wall": bool(value.get("verification_wall")),
        "inaccessible": bool(value.get("inaccessible")),
    }


def validate_bridge_url(url: str) -> str:
    """Validate a loopback WebSocket endpoint without echoing credentials."""
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError("XHS Bridge URL must use ws or wss")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("XHS Bridge must remain on loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("XHS Bridge URL cannot contain credentials or query parameters")
    return url


def validate_method(method: str) -> str:
    normalized = str(method or "")
    if normalized not in SAFE_METHODS:
        raise ValueError(f"Bridge method is not allowed: {normalized or '[empty]'}")
    return normalized


def validate_xhs_page_url(url: str) -> str:
    """Accept only ordinary xiaohongshu page URLs; temporary query context is rejected."""
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise ValueError("XHS page URL must be an https xiaohongshu.com URL")
    if parsed.query or parsed.fragment:
        raise ValueError("XHS Bridge does not accept temporary URL parameters")
    return url
