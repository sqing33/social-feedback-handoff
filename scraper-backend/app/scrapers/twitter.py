"""Twitter / X 抓取器（API v2）。

需要 TWITTER_BEARER_TOKEN 环境变量。
不需要用户 cookie。

- 推文: GET https://api.twitter.com/2/tweets/:id?tweet.fields=public_metrics,created_at
- 用户: GET https://api.twitter.com/2/users/:id?user.fields=public_metrics
- 搜索: GET https://api.twitter.com/2/tweets/search/recent?query=...
- 评论: 通过 search query `conversation_id:ID` 间接
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .base import BaseScraper
from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.twitter")

_API = "https://api.twitter.com/2"


def _bearer() -> str:
    return os.environ.get("TWITTER_BEARER_TOKEN", "").strip()


def _require_bearer() -> str:
    t = _bearer()
    if not t:
        raise ScraperError(
            "TWITTER_BEARER_TOKEN env is required for Twitter/X scraping. "
            "Get a token at https://developer.twitter.com → Projects & Apps → Keys and tokens",
            code=ErrorCode.SERVICE_UNAUTHORIZED,
            status_code=401,
            retryable=False,
            capability="api-key",
        )
    return t


def _iso_to_ms(iso: str) -> int:
    """ISO8601 -> 毫秒时间戳。空字符串返回 0。"""
    if not iso:
        return 0
    try:
        from datetime import datetime, timezone
        # 形如 2024-09-12T10:00:00.000Z
        s = iso.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


class TwitterScraper(BaseScraper):
    platform = "twitter"
    degrades_to_mock = False

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {_require_bearer()}",
            "User-Agent": "Sonar-Scraper/1.0",
        }

    async def get_info(self, content_id: str) -> dict:
        async with self._client() as c:
            r = await c.get(
                f"{_API}/tweets/{content_id}",
                params={
                    "tweet.fields": "public_metrics,created_at,author_id",
                    "expansions": "author_id",
                    "user.fields": "username,name,public_metrics",
                },
                headers=self._headers(),
            )
            d = r.json()
            if "errors" in d or "error" in d:
                err = d.get("errors") or d.get("error") or {}
                code = err.get("code") if isinstance(err, dict) else 0
                raise ScraperError(
                    f"twitter info failed: {err.get('message') or 'unknown'}",
                    code=ErrorCode.SCRAPE_ERROR if code not in (401, 403) else ErrorCode.CREDENTIAL_EXPIRED,
                    status_code=code if code in (401, 403, 404) else 500,
                    retryable=False,
                    capability="info",
                )
            data = d.get("data") or {}
            if not data:
                raise ScraperError(
                    f"twitter tweet not found: {content_id}",
                    code=ErrorCode.CONTENT_NOT_FOUND,
                    status_code=404,
                    retryable=False,
                    capability="info",
                )
            m = data.get("public_metrics", {}) or {}
            author = ""
            for u in (d.get("includes") or {}).get("users") or []:
                if u.get("id") == data.get("author_id"):
                    author = u.get("username") or u.get("name") or ""
                    break
            return {
                "title": data.get("text", "")[:120],
                "user": {"nickname": author},
                "interact_info": {
                    "liked_count": int(m.get("like_count", 0)),
                    "collected_count": int(m.get("bookmark_count", 0)),
                    "comment_count": int(m.get("reply_count", 0)),
                },
            }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        async with self._client() as c:
            r = await c.get(
                f"{_API}/tweets/search/recent",
                params={
                    "query": f"conversation_id:{content_id} -is:retweet",
                    "max_results": min(100, max(max_comments, 10)),
                    "tweet.fields": "public_metrics,created_at,author_id",
                    "expansions": "author_id",
                    "user.fields": "username",
                },
                headers=self._headers(),
            )
            d = r.json()
            if "error" in d or "errors" in d:
                err = d.get("error") or (d.get("errors") or [{}])[0]
                # search/recent 需要 Basic+，否则 403
                raise ScraperError(
                    f"twitter comments failed: {err.get('message') or 'unknown'}",
                    code=ErrorCode.CREDENTIAL_EXPIRED,
                    status_code=403,
                    retryable=False,
                    capability="comments",
                )
            out: list[dict] = []
            for tw in (d.get("data") or [])[:max_comments]:
                author = ""
                for u in (d.get("includes") or {}).get("users") or []:
                    if u.get("id") == tw.get("author_id"):
                        author = u.get("username") or ""
                        break
                m = tw.get("public_metrics", {}) or {}
                out.append(
                    {
                        "user_info": {"nickname": author},
                        "content": tw.get("text", ""),
                        "like_count": int(m.get("like_count", 0)),
                        "create_time": _iso_to_ms(tw.get("created_at", "")),
                        "sub_comments": [],
                    }
                )
            return out

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        async with self._client() as c:
            r = await c.get(
                f"{_API}/tweets/search/recent",
                params={
                    "query": f"{keyword} -is:retweet lang:en",
                    "max_results": min(100, max(10, limit)),
                    "tweet.fields": "public_metrics,created_at,author_id",
                    "expansions": "author_id",
                    "user.fields": "username,name,public_metrics",
                },
                headers=self._headers(),
            )
            d = r.json()
            if "error" in d or "errors" in d:
                err = d.get("error") or (d.get("errors") or [{}])[0]
                raise ScraperError(
                    f"twitter search failed: {err.get('message') or 'unknown'}",
                    code=ErrorCode.CREDENTIAL_EXPIRED if err.get("code") == 403 else ErrorCode.SCRAPE_ERROR,
                    status_code=err.get("code") or 500,
                    retryable=False,
                    capability="search",
                )
            out: list[dict] = []
            users_index: dict[str, dict] = {}
            for u in (d.get("includes") or {}).get("users") or []:
                users_index[u.get("id")] = u
            for tw in (d.get("data") or [])[:limit]:
                uid = tw.get("author_id")
                u = users_index.get(uid, {})
                m = tw.get("public_metrics", {}) or {}
                out.append(
                    {
                        "content_id": tw.get("id", ""),
                        "title": (tw.get("text") or "")[:120],
                        "author": u.get("username") or u.get("name") or "",
                        "url": f"https://x.com/{u.get('username','')}/status/{tw.get('id','')}" if u.get("username") else "",
                        "stats": {
                            "view": int(m.get("impression_count", 0)),
                            "like": int(m.get("like_count", 0)),
                            "reply": int(m.get("reply_count", 0)),
                        },
                        "tweet_id": tw.get("id"),
                    }
                )
            return out
