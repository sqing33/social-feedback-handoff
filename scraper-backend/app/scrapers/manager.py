"""抓取器调度：把 platform 字符串映射到具体实现。"""

from __future__ import annotations

from typing import Optional

from .base import BaseScraper
from .bilibili import BilibiliScraper
from .cookies import get_cookie
from .douyin import DouyinScraper
from .kuaishou import KuaishouScraper
from .tiktok import TikTokScraper
from .twitter import TwitterScraper
from .xhs import XhsScraper
from .youtube import YoutubeScraper


def get_scraper(
    platform: str,
    cookie: Optional[str] = None,
    proxy: Optional[str] = None,
) -> BaseScraper:
    """根据平台名返回抓取器实例。

    优先级：调用方 cookie（auth_context）> 进程内 store > env COOKIES_<PLATFORM>
    """
    if not cookie:
        cookie = get_cookie(platform)
    cls = {
        "bilibili": BilibiliScraper,
        "kuaishou": KuaishouScraper,
        "tiktok": TikTokScraper,
        "xhs": XhsScraper,
        "douyin": DouyinScraper,
        "twitter": TwitterScraper,
        "youtube": YoutubeScraper,
    }.get(platform)
    if not cls:
        raise ValueError(f"unknown platform: {platform}")
    return cls(cookies=cookie, proxy=proxy)
