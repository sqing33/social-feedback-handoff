"""所有平台抓取器的抽象基类。

设计：
- 平台抓取器返回统一的 NoteInfo / UnifiedComment / SearchResult 字典
- 抓取失败抛 ScraperError（带正确错误码），由上层 router 决定降级还是回传
- get_cookies() 从 app.scrapers.cookies 拿（用户填的 COOKIES.json 或 .env）
- 标志：DEGRADABLE 表示没 cookie 时可以降级 mock；REAL_ONLY 表示必须有 cookie 否则报错
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.platform")


HEADERS_COMMON = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class BaseScraper(ABC):
    platform: str = "base"
    degrades_to_mock: bool = True  # 无 cookie 时是否降级 mock

    def __init__(self, cookies: Optional[str] = None, proxy: Optional[str] = None):
        self.cookies = cookies or ""
        self.proxy = proxy

    def _client(self, timeout: float = 12.0) -> httpx.AsyncClient:
        headers = dict(HEADERS_COMMON)
        if self.cookies:
            headers["Cookie"] = self.cookies
        if self.platform == "bilibili":
            headers["Referer"] = "https://www.bilibili.com"
        elif self.platform == "kuaishou":
            headers["Referer"] = "https://www.kuaishou.com"
        elif self.platform == "xhs":
            headers["Referer"] = "https://www.xiaohongshu.com"
        elif self.platform == "douyin":
            headers["Referer"] = "https://www.douyin.com"
        return httpx.AsyncClient(timeout=timeout, headers=headers, proxy=self.proxy)

    @abstractmethod
    async def get_info(self, content_id: str) -> dict:
        ...

    @abstractmethod
    async def get_comments(self, content_id: str, **kwargs) -> list[dict]:
        ...

    @abstractmethod
    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        ...

    async def get_user_contents(
        self,
        identifier: str,
        *,
        account_name: str = "",
        limit: int = 20,
    ) -> list[dict]:
        """Return real posts from one account/profile.

        Platform scrapers that support account sync must override this method.
        The default is an explicit capability error; it must never degrade to
        keyword search because that can mix content from namesake accounts.
        """
        raise ScraperError(
            f"{self.platform} account content discovery is not implemented",
            code=ErrorCode.SCRAPE_ERROR,
            status_code=501,
            retryable=False,
            capability="account-contents",
        )

    @staticmethod
    def _require_cookie(platform: str) -> None:
        """给需要 cookie 的平台统一报错。"""
        raise ScraperError(
            f"{platform} requires a login cookie. Set COOKIES_{platform.upper()} env or "
            f"provide cookie via the request auth_context.cookie",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="cookie-required",
        )
