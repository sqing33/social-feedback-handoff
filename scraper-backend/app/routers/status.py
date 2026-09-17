"""GET /api/status — 真实 scraper 与浏览器状态。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..browser.pool import pool
from ..platforms import PLATFORMS

router = APIRouter()

LOGIN_REQUIRED_CAPABILITIES = {
    "xhs": {"comments", "info", "search"},
    "kuaishou": {"comments", "info", "search"},
    "tiktok": {"comments", "info", "search"},
    "twitter": {"comments", "info", "search"},
    "youtube": {"comments", "info", "search"},
    # Douyin detail/comments are public in this implementation; search is not.
    "douyin": {"search"},
}
CAPABILITIES = ("comments", "info", "search")


def _capability_status(status: str, active_backend: str | None, error: str | None) -> dict:
    return {
        "status": status,
        "active_backend": active_backend,
        "ordered_backends": [active_backend] if active_backend else [],
        "error": error,
    }


@router.get("/api/status")
async def get_status(request: Request) -> dict:
    rid = getattr(request.state, "request_id", None)
    browser = await pool.status()
    browser_ready = bool(browser.get("ready"))
    logged_in = browser.get("logged_in") or {}
    browser_error = browser.get("init_error")
    statuses: dict[str, dict] = {}

    for platform in PLATFORMS:
        capabilities: dict[str, dict] = {}
        for capability_name in CAPABILITIES:
            error: str | None = None
            if not browser_ready:
                capability_status = "unavailable"
                error = browser_error or "browser is not ready"
            elif capability_name in LOGIN_REQUIRED_CAPABILITIES.get(platform, set()) and not logged_in.get(platform, False):
                capability_status = "needs_login"
                error = "active login session is required"
            else:
                capability_status = "healthy"
            active_backend = f"{platform}-real" if capability_status == "healthy" else None
            capabilities[capability_name] = _capability_status(capability_status, active_backend, error)

        capability_states = {item["status"] for item in capabilities.values()}
        if "healthy" in capability_states and "needs_login" in capability_states:
            platform_status = "partial"
        elif "healthy" in capability_states:
            platform_status = "healthy"
        elif "needs_login" in capability_states:
            platform_status = "needs_login"
        else:
            platform_status = "unavailable"
        platform_error = None
        if platform_status == "needs_login":
            platform_error = "active login session is required"
        elif platform_status == "partial":
            platform_error = "some capabilities require an active login session"
        elif platform_status == "unavailable":
            platform_error = browser_error or "browser is not ready"
        statuses[platform] = {
            "available": platform_status in {"healthy", "partial"},
            "error": platform_error,
            "status": platform_status,
            "active_backend": f"{platform}-real" if platform_status in {"healthy", "partial"} else None,
            "capabilities": capabilities,
        }

    return {
        "status": statuses,
        "browser": browser,
        "request_id": rid,
        "version": "1.0.0",
    }
