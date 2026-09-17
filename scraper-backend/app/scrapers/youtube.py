"""YouTube 抓取器（Data API v3）。

需要 YOUTUBE_API_KEY 环境变量。
不需要用户 cookie。

- 搜索: GET https://www.googleapis.com/youtube/v3/search?part=snippet&q=...&type=video&key=...
- 详情: GET https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id=...&key=...
- 评论: GET https://www.googleapis.com/youtube/v3/commentThreads?part=snippet&videoId=...&key=...
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .base import BaseScraper
from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.youtube")

_API = "https://www.googleapis.com/youtube/v3"


def _api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "").strip()


def _require_key() -> str:
    k = _api_key()
    if not k:
        raise ScraperError(
            "YOUTUBE_API_KEY env is required for YouTube scraping. "
            "Get a key at https://console.cloud.google.com → APIs & Services → YouTube Data API v3",
            code=ErrorCode.SERVICE_UNAUTHORIZED,
            status_code=401,
            retryable=False,
            capability="api-key",
        )
    return k


class YoutubeScraper(BaseScraper):
    platform = "youtube"
    degrades_to_mock = False

    async def _search(self, c, keyword: str, limit: int) -> list[dict]:
        r = await c.get(
            f"{_API}/search",
            params={
                "part": "snippet",
                "q": keyword,
                "type": "video",
                "maxResults": min(50, limit),
                "key": _require_key(),
            },
        )
        d = r.json()
        if "error" in d:
            raise ScraperError(
                f"youtube search error: {d['error'].get('message')}",
                code=ErrorCode.SCRAPE_ERROR,
                status_code=500,
                retryable=False,
                capability="search",
            )
        out: list[dict] = []
        for item in d.get("items", []):
            vid = item.get("id", {}).get("videoId", "")
            sn = item.get("snippet", {})
            out.append(
                {
                    "content_id": vid,
                    "title": sn.get("title", ""),
                    "author": sn.get("channelTitle", ""),
                    "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
                    "stats": {"view": 0, "like": 0, "reply": 0},
                    "video_id": vid,
                }
            )
        return out

    async def get_info(self, content_id: str) -> dict:
        async with self._client() as c:
            r = await c.get(
                f"{_API}/videos",
                params={
                    "part": "snippet,statistics",
                    "id": content_id,
                    "key": _require_key(),
                },
            )
            d = r.json()
            if "error" in d:
                raise ScraperError(
                    f"youtube info error: {d['error'].get('message')}",
                    code=ErrorCode.SCRAPE_ERROR,
                    status_code=500,
                    retryable=False,
                    capability="info",
                )
            items = d.get("items") or []
            if not items:
                raise ScraperError(
                    f"youtube video not found: {content_id}",
                    code=ErrorCode.CONTENT_NOT_FOUND,
                    status_code=404,
                    retryable=False,
                    capability="info",
                )
            v = items[0]
            sn = v.get("snippet", {})
            st = v.get("statistics", {})
            return {
                "title": sn.get("title", ""),
                "user": {"nickname": sn.get("channelTitle", "")},
                "interact_info": {
                    "liked_count": int(st.get("likeCount", 0)),
                    "collected_count": int(st.get("favoriteCount", 0)),
                    # YouTube 用播放数代替收藏数
                    "comment_count": int(st.get("commentCount", 0)),
                },
            }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        async with self._client() as c:
            r = await c.get(
                f"{_API}/commentThreads",
                params={
                    "part": "snippet",
                    "videoId": content_id,
                    "maxResults": min(100, max_comments),
                    "order": "relevance",
                    "textFormat": "plainText",
                    "key": _require_key(),
                },
            )
            d = r.json()
            if "error" in d:
                # 评论可能关闭（403 commentsDisabled），降级返回空
                err = d["error"]
                if err.get("code") in (403, 400):
                    logger.info("youtube comments disabled or unavailable: %s", err.get("message"))
                    return []
                raise ScraperError(
                    f"youtube comments error: {err.get('message')}",
                    code=ErrorCode.SCRAPE_ERROR,
                    status_code=500,
                    retryable=False,
                    capability="comments",
                )
            out: list[dict] = []
            for it in d.get("items", [])[:max_comments]:
                top = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                published = top.get("publishedAt", "")
                create_ms = 0
                if published:
                    try:
                        from datetime import datetime
                        s = published.replace("Z", "+00:00")
                        create_ms = int(datetime.fromisoformat(s).timestamp() * 1000)
                    except Exception:
                        create_ms = 0
                out.append(
                    {
                        "user_info": {"nickname": top.get("authorDisplayName", "")},
                        "content": top.get("textDisplay", ""),
                        "like_count": int(top.get("likeCount", 0)),
                        "create_time": create_ms,
                        "sub_comments": [],
                    }
                )
            return out

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        async with self._client() as c:
            return await self._search(c, keyword, limit)
