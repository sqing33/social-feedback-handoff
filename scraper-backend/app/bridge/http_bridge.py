"""HTTP 版 Chrome 扩展桥接。

协议：扩展定期 POST 当前 cookie 状态到 /api/bridge/cookies
- 不需要 WebSocket 库，零依赖
- 扩展每 30s 推一次
- 后端把 cookie 拼成字符串存到 cookie_store
- scraper 调用时从 store 取
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..scrapers import cookies as cookie_store

logger = logging.getLogger("scraper.bridge")

router = APIRouter()

DOMAIN_TO_PLATFORM = {
    ".xiaohongshu.com": "xhs",
    ".douyin.com": "douyin",
    ".tiktok.com": "tiktok",
    ".kuaishou.com": "kuaishou",
    ".x.com": "twitter",
    ".twitter.com": "twitter",
    ".bilibili.com": "bilibili",
    ".youtube.com": "youtube",
}


class CookieItem(BaseModel):
    name: str
    value: str
    domain: str


class PushBody(BaseModel):
    domains: list[str] = []
    cookies: list[CookieItem] = []


@router.post("/api/bridge/cookies")
async def push_cookies(body: PushBody, request: Request) -> dict:
    """Chrome 扩展推送 cookie。

    扩展把 chrome.cookies.getAll 拿到的结果 POST 过来。
    """
    pushed: dict[str, int] = {}
    for c in body.cookies:
        # 通过 domain 找平台
        platform = None
        for d, p in DOMAIN_TO_PLATFORM.items():
            if c.domain.endswith(d.lstrip(".")):
                platform = p
                break
        if not platform:
            continue
        # 同一个 platform 可能来自多个 domain，合并
        # 这里简化：每次 push 覆盖前一个
        # 通过 cookie_store 维护 per-domain 缓存
        from ..scrapers.cookies import set_cookie, get_cookie
        existing = get_cookie(platform) or ""
        parts = [p for p in existing.split("; ") if p and not p.startswith(f"{c.name}=")]
        parts.append(f"{c.name}={c.value}")
        new_cookie = "; ".join(parts)
        set_cookie(platform, new_cookie)
        pushed[platform] = pushed.get(platform, 0) + 1

    rid = getattr(request.state, "request_id", None)
    logger.info("extension pushed cookies: %s", pushed)
    return {
        "ok": True,
        "received": len(body.cookies),
        "by_platform": pushed,
        "request_id": rid,
    }


@router.get("/api/bridge/status")
def bridge_status() -> dict:
    from ..scrapers.cookies import get_cookie
    return {
        "platforms_with_cookies": [
            p for p in DOMAIN_TO_PLATFORM.values() if get_cookie(p)
        ],
    }
