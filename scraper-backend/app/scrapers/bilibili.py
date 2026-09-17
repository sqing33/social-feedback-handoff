"""B 站抓取器。

完全免鉴权可用的三个 API：
- 搜索: GET /x/web-interface/search/all/v2?keyword=...
- 视频详情: GET /x/web-interface/view?bvid=...
- 评论: GET /x/v2/reply/main?type=1&oid=<aid>&mode=3

B 站的视频 ID 体系：bvid -> aid 通过 /x/web-interface/view 拿。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .base import HEADERS_COMMON, BaseScraper
from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.bilibili")

_API = "https://api.bilibili.com"


def _extract_bvid(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("BV"):
        return s
    m = re.search(r"(BV[A-Za-z0-9]+)", s)
    return m.group(1) if m else s


class BilibiliScraper(BaseScraper):
    platform = "bilibili"
    degrades_to_mock = False  # 免鉴权可用，不需要降级

    async def _resolve_aid(self, content_id: str) -> int:
        bvid = _extract_bvid(content_id)
        async with self._client() as c:
            r = await c.get(f"{_API}/x/web-interface/view", params={"bvid": bvid})
            data = r.json()
            if data.get("code") != 0 or not data.get("data"):
                raise ScraperError(
                    f"bilibili view not found: {content_id} (code={data.get('code')})",
                    code=ErrorCode.CONTENT_NOT_FOUND,
                    status_code=404,
                    retryable=False,
                    capability="info",
                )
            return int(data["data"]["aid"])

    async def get_info(self, content_id: str) -> dict:
        bvid = _extract_bvid(content_id)
        async with self._client() as c:
            r = await c.get(f"{_API}/x/web-interface/view", params={"bvid": bvid})
            data = r.json()
            if data.get("code") != 0 or not data.get("data"):
                raise ScraperError(
                    f"bilibili view failed: {data.get('message') or 'unknown'}",
                    code=ErrorCode.CONTENT_NOT_FOUND,
                    status_code=404,
                    retryable=False,
                    capability="info",
                )
            d = data["data"]
            return {
                "title": d.get("title", ""),
                "user": {"nickname": d.get("owner", {}).get("name", "")},
                "interact_info": {
                    "liked_count": d.get("stat", {}).get("like", 0),
                    "collected_count": d.get("stat", {}).get("favorite", 0),
                    "comment_count": d.get("stat", {}).get("reply", 0),
                },
            }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        aid = await self._resolve_aid(content_id)
        async with self._client() as c:
            # mode 3 = 热门；先试热门，没有则试 mode=2（按时间）
            replies: list[dict] = []
            for mode in (3, 2):
                r = await c.get(
                    f"{_API}/x/v2/reply/main",
                    params={"type": 1, "oid": aid, "mode": mode, "ps": min(20, max_comments)},
                )
                data = r.json()
                if data.get("code") != 0:
                    logger.warning("bilibili reply code=%s msg=%s", data.get("code"), data.get("message"))
                    continue
                replies = ((data.get("data") or {}).get("replies") or [])
                if replies:
                    break
            out: list[dict] = []
            for r in replies[:max_comments]:
                member = r.get("member", {})
                ctime = r.get("ctime", 0) * 1000
                subs_raw = r.get("replies") or []
                subs = [
                    {
                        "user_info": {"nickname": (s.get("member") or {}).get("uname", "")},
                        "content": (s.get("content") or {}).get("message", ""),
                        "like_count": s.get("like", 0),
                        "create_time": (s.get("ctime", 0) or 0) * 1000,
                    }
                    for s in subs_raw[:3]
                ]
                out.append(
                    {
                        "user_info": {"nickname": member.get("uname", "")},
                        "content": (r.get("content") or {}).get("message", ""),
                        "like_count": r.get("like", 0),
                        "create_time": ctime,
                        "sub_comments": subs,
                    }
                )
            return out

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        async with self._client() as c:
            r = await c.get(
                f"{_API}/x/web-interface/search/all/v2",
                params={"keyword": keyword, "page": 1, "page_size": min(50, limit)},
            )
            data = r.json()
            if data.get("code") != 0:
                raise ScraperError(
                    f"bilibili search failed: {data.get('message') or 'unknown'}",
                    code=ErrorCode.SCRAPE_ERROR,
                    status_code=500,
                    retryable=True,
                    capability="search",
                )
            # /all/v2 当前结构：result 是 [{ result_type: "video", data: [...] }, ...]
            video_items: list[dict] = []
            for group in ((data.get("data") or {}).get("result") or []):
                if group.get("result_type") == "video" and isinstance(group.get("data"), list):
                    video_items.extend(group["data"])
            if not video_items:
                # 兜底：直接遍历 data.result 找含 bvid 的
                for group in ((data.get("data") or {}).get("result") or []):
                    for v in (group.get("data") or []):
                        if v.get("bvid"):
                            video_items.append(v)

            out: list[dict] = []
            for v in video_items[:limit]:
                bvid = v.get("bvid") or (f"BV{v.get('id')}" if v.get("id") else "")
                title = (v.get("title") or "").replace("<em class=\"keyword\">", "").replace("</em>", "").replace("<em>", "").replace("</em>", "")
                out.append(
                    {
                        "content_id": bvid,
                        "title": title,
                        "author": v.get("author") or (str(v.get("mid")) if v.get("mid") else ""),
                        "url": v.get("arcurl") or (f"https://www.bilibili.com/video/{bvid}" if bvid else ""),
                        "stats": {
                            "view": int(v.get("play") or 0),
                            "like": int(v.get("like") or 0),
                            "reply": int(v.get("review") or 0),
                        },
                        "bvid": bvid,
                    }
                )
            return out
