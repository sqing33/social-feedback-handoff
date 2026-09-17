"""POST /api/reload/{platform} — 重载平台爬虫（§5.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import ErrorCode, ScraperError
from ..mock import PLATFORMS

router = APIRouter()


@router.post("/api/reload/{platform}")
def reload_platform(platform: str, request: Request) -> dict:
    if platform not in PLATFORMS:
        raise ScraperError(
            f"Cannot reload unknown platform: {platform}",
            code=ErrorCode.PLATFORM_UNAVAILABLE,
            status_code=503,
            retryable=False,
            capability="reload",
        )
    return {
        "platform": platform,
        "success": True,
        "request_id": getattr(request.state, "request_id", None),
    }
