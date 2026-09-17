"""TikTok 抓取器（基于 Playwright）。

TikTok web SPA 把 API 响应塞到 window.SIGI_STATE（搜索）/ ItemModule（详情）。
需要登录态 cookie（ttwid / sessionid），用 Playwright 持久化浏览器复用。

未登录时抛 CREDENTIAL_EXPIRED，前端提示 POST /api/browser/login/tiktok。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseScraper
from ..browser.pool import pool
from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.tiktok")

_SEARCH_URL = "https://www.tiktok.com/search?q={keyword}"
_DETAIL_URL = "https://www.tiktok.com/@_/video/{aweme_id}"

# 搜索页：等 SIGI_STATE 出现
_SEARCH_WAIT_JS = """
() => {
  if (window.SIGI_STATE && window.SIGI_STATE.ItemModule) {
    return Object.keys(window.SIGI_STATE.ItemModule).length > 0;
  }
  return document.querySelectorAll('[data-e2e="search-card"]').length > 0;
}
"""

# 提取搜索结果
_EXTRACT_SEARCH_JS = """
() => {
  const sigi = window.SIGI_STATE || {};
  const items = sigi.ItemModule || {};
  return Object.values(items).slice(0, 30).map(it => ({
    id: it.id,
    desc: (it.desc || '').replace(/<[^>]+>/g, ''),
    author: it.author || it.authorId,
    author_id: it.authorId,
    video: it.video || {},
    stats: it.stats || {},
    create_time: it.createTime || 0,
  }));
}
"""

# 详情页
_DETAIL_WAIT_JS = """
() => {
  if (window.SIGI_STATE && window.SIGI_STATE.ItemModule) {
    const items = window.SIGI_STATE.ItemModule;
    return Object.keys(items).length > 0;
  }
  return document.querySelector('video') !== null;
}
"""

_EXTRACT_DETAIL_JS = """
() => {
  const sigi = window.SIGI_STATE || {};
  const items = sigi.ItemModule || {};
  const first = Object.values(items)[0];
  if (!first) return null;
  return {
    id: first.id,
    desc: (first.desc || '').replace(/<[^>]+>/g, ''),
    author: first.author || first.authorId,
    stats: first.stats || {},
    create_time: first.createTime || 0,
  };
}
"""

# 评论
_COMMENT_WAIT_JS = """
() => {
  if (window.SIGI_STATE && window.SIGI_STATE.CommentItemModule) {
    return Object.keys(window.SIGI_STATE.CommentItemModule).length > 0;
  }
  return document.querySelectorAll('[class*="Comment"]').length > 0;
}
"""

_EXTRACT_COMMENTS_JS = """
() => {
  const sigi = window.SIGI_STATE || {};
  const cmod = sigi.CommentItemModule || {};
  return Object.values(cmod).slice(0, 30).map(c => ({
    nickname: c.user?.nickname || c.user?.uniqueId || '',
    content: c.text || c.share_info?.description || '',
    like_count: c.digg_count || c.like_count || 0,
    create_time: c.create_time || 0,
  }));
}
"""


async def _check_login_or_raise() -> None:
    cookies = await pool.get_cookies("tiktok")
    if not pool.is_logged_in("tiktok", cookies):
        raise ScraperError(
            "tiktok requires login. Call POST /api/browser/login/tiktok first to authenticate.",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="browser-login",
        )


class TikTokScraper(BaseScraper):
    platform = "tiktok"
    degrades_to_mock = False

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        await _check_login_or_raise()
        url = _SEARCH_URL.format(keyword=keyword)
        await pool.navigate("tiktok", url, wait_until="domcontentloaded", timeout_ms=25000)
        try:
            await pool.wait_for_state("tiktok", _SEARCH_WAIT_JS, timeout_ms=12000)
        except ScraperError:
            pass
        items = await pool.evaluate("tiktok", _EXTRACT_SEARCH_JS, timeout_ms=10000)
        if not isinstance(items, list):
            return []
        out: list[dict] = []
        for it in items[:limit]:
            vid = it.get("id", "")
            stats = it.get("stats") or {}
            out.append(
                {
                    "content_id": vid,
                    "title": (it.get("desc") or "")[:120],
                    "author": it.get("author", ""),
                    "url": f"https://www.tiktok.com/@_/video/{vid}",
                    "stats": {
                        "view": int(stats.get("playCount", 0) or 0),
                        "like": int(stats.get("diggCount", 0) or 0),
                        "reply": int(stats.get("commentCount", 0) or 0),
                    },
                    "video_id": vid,
                }
            )
        return out

    async def get_info(self, content_id: str) -> dict:
        await _check_login_or_raise()
        url = _DETAIL_URL.format(aweme_id=content_id)
        await pool.navigate("tiktok", url, wait_until="domcontentloaded", timeout_ms=25000)
        try:
            await pool.wait_for_state("tiktok", _DETAIL_WAIT_JS, timeout_ms=12000)
        except ScraperError:
            pass
        v = await pool.evaluate("tiktok", _EXTRACT_DETAIL_JS, timeout_ms=10000)
        if not v:
            raise ScraperError(
                f"tiktok info not found: {content_id}",
                code=ErrorCode.CONTENT_NOT_FOUND,
                status_code=404,
                retryable=False,
                capability="info",
            )
        stats = v.get("stats") or {}
        return {
            "title": (v.get("desc") or "")[:120],
            "user": {"nickname": v.get("author", "")},
            "interact_info": {
                "liked_count": int(stats.get("diggCount", 0) or 0),
                "collected_count": int(stats.get("collectCount", 0) or 0),
                "comment_count": int(stats.get("commentCount", 0) or 0),
                "share_count": int(stats.get("shareCount", 0) or 0),
            },
        }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        await _check_login_or_raise()
        url = _DETAIL_URL.format(aweme_id=content_id)
        await pool.navigate("tiktok", url, wait_until="domcontentloaded", timeout_ms=25000)
        try:
            await pool.wait_for_state("tiktok", _DETAIL_WAIT_JS, timeout_ms=10000)
        except ScraperError:
            pass
        try:
            await pool.evaluate(
                "tiktok",
                "() => { window.scrollTo(0, document.body.scrollHeight * 0.6); return true; }",
                timeout_ms=5000,
            )
        except Exception:
            pass
        try:
            await pool.wait_for_state("tiktok", _COMMENT_WAIT_JS, timeout_ms=8000)
        except ScraperError:
            pass
        comments = await pool.evaluate("tiktok", _EXTRACT_COMMENTS_JS, timeout_ms=10000)
        if not isinstance(comments, list):
            return []
        out: list[dict] = []
        for c in comments[:max_comments]:
            out.append(
                {
                    "user_info": {"nickname": c.get("nickname", "")},
                    "content": c.get("content", ""),
                    "like_count": int(c.get("like_count", 0) or 0),
                    "create_time": int(c.get("create_time", 0) or 0) * 1000,
                    "sub_comments": [],
                }
            )
        return out
