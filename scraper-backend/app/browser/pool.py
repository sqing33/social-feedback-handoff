"""Playwright 浏览器池。

设计：
- 启动一个 Chromium 进程 + 持久化 user data dir
- 每个平台懒加载一个 page（tab），持久保留以便复用 cookie
- `pool.navigate(platform, url)` 切换到该平台 page，导航到 url
- `pool.evaluate(platform, js)` 在该平台 page 上执行 JS
- `pool.get_cookies(platform)` 拿该平台已登录 cookie
- 抓取器抛错时上层 fallback 或上报
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from ..errors import ErrorCode, ScraperError
from ..xhs_bridge.client import SafeBridgeError, bridge_from_environment
from ..xhs_bridge.runtime import enabled as xhs_bridge_enabled, prepare_extension

logger = logging.getLogger("scraper.browser")

# 浏览器 user data dir（持久化 cookie / localStorage）
_USER_DATA_DIR = Path(
    os.environ.get(
        "BROWSER_USER_DATA_DIR",
        str(Path.home() / ".minimax" / "social-feedback" / "browser"),
    )
).resolve()

# 是否 headless（默认 headless=True；用户首次登录时切到 False）
_HEADLESS = os.environ.get("BROWSER_HEADLESS", "true").lower() in ("1", "true", "yes")
_BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "").strip()
_BROWSER_EXECUTABLE_PATH = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
_BROWSER_USER_AGENT = os.environ.get("BROWSER_USER_AGENT", "").strip()
_BROWSER_PROXY_SERVER = os.environ.get("BROWSER_PROXY_SERVER", "").strip()


def _parse_macos_proxy_config(raw: str) -> str:
    """Return a non-credentialed proxy URL from ``scutil --proxy`` output."""
    text = str(raw or "")
    for prefix in ("HTTPS", "HTTP"):
        enabled = re.search(rf"^\s*{prefix}Enable\s*:\s*1\s*$", text, re.M)
        host = re.search(rf"^\s*{prefix}Proxy\s*:\s*([^\s]+)\s*$", text, re.M)
        port = re.search(rf"^\s*{prefix}Port\s*:\s*(\d+)\s*$", text, re.M)
        if enabled and host and port:
            return f"http://{host.group(1)}:{int(port.group(1))}"
    return ""


def _detect_macos_proxy() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        completed = subprocess.run(
            ["/usr/sbin/scutil", "--proxy"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    return _parse_macos_proxy_config(completed.stdout) if completed.returncode == 0 else ""


_EFFECTIVE_BROWSER_PROXY = _BROWSER_PROXY_SERVER or _detect_macos_proxy()

# 启动 Chromium 内核浏览器的 args（支持 Playwright Chromium / Chrome / Microsoft Edge）
_DEFAULT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]

# 平台登录 URL（用于 login 接口）
LOGIN_URLS = {
    "xhs": "https://www.xiaohongshu.com",
    "douyin": "https://www.douyin.com",
    "kuaishou": "https://www.kuaishou.com",
    "tiktok": "https://www.tiktok.com/login",
    "bilibili": "https://passport.bilibili.com/login",
    "twitter": "https://x.com/login",
    "youtube": "https://accounts.google.com/signin",
}

# 平台根域（用于判断是否已登录）
PLATFORM_HOME = {
    "xhs": "xiaohongshu.com",
    "douyin": "douyin.com",
    "kuaishou": "kuaishou.com",
    "tiktok": "tiktok.com",
    "bilibili": "bilibili.com",
    "twitter": "x.com",
    "youtube": "youtube.com",
}


def _safe_url_for_error(url: str) -> str:
    """Redact temporary access context before URLs reach errors or logs."""
    try:
        parsed = urlsplit(str(url or ""))
        sensitive = {"xsec_token", "xsec_source", "token", "access_token", "api_key", "key"}
        query = [
            (key, "[redacted]" if key.lower() in sensitive else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    except Exception:
        return "[invalid-url]"


class BrowserPool:
    """共享的 Chromium 浏览器池。"""

    def __init__(self) -> None:
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._keepalive_page: Optional[Page] = None
        self._pages: dict[str, Page] = {}
        self._xhs_active_page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._init_error: Optional[str] = None
        self._xhs_bridge = bridge_from_environment()

    async def start(self) -> None:
        """启动浏览器（幂等）。"""
        async with self._lock:
            if self._ready:
                return
            try:
                _USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
                self._pw = await async_playwright().start()
                launch_options = {
                    "user_data_dir": str(_USER_DATA_DIR),
                    "headless": _HEADLESS,
                    "args": list(_DEFAULT_ARGS),
                    "viewport": {"width": 1920, "height": 1080},
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                    "ignore_https_errors": True,
                }
                if xhs_bridge_enabled():
                    extension_path = prepare_extension()
                    if extension_path:
                        launch_options["args"].append(f"--load-extension={extension_path}")
                        launch_options["ignore_default_args"] = ["--disable-extensions"]
                        logger.info("safe XHS extension enabled for this browser process")
                if _EFFECTIVE_BROWSER_PROXY:
                    launch_options["proxy"] = {
                        "server": _EFFECTIVE_BROWSER_PROXY,
                        "bypass": "127.0.0.1,localhost",
                    }
                if _BROWSER_EXECUTABLE_PATH:
                    launch_options["executable_path"] = _BROWSER_EXECUTABLE_PATH
                elif _BROWSER_CHANNEL:
                    launch_options["channel"] = _BROWSER_CHANNEL
                if _BROWSER_USER_AGENT:
                    launch_options["user_agent"] = _BROWSER_USER_AGENT
                elif not (_BROWSER_EXECUTABLE_PATH or _BROWSER_CHANNEL):
                    launch_options["user_agent"] = (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    )
                # launch_persistent_context：保持独立用户目录，可指定系统 Edge/Chrome。
                self._context = await self._pw.chromium.launch_persistent_context(**launch_options)
                self._browser = self._context.browser
                # Chrome may restore scraper-owned search/detail tabs from the
                # previous process. Create one neutral keepalive page first, then
                # close only the restored pages. Closing the last page would exit
                # Chrome and make the persisted login appear to vanish at runtime.
                startup_pages = list(self._context.pages)
                self._keepalive_page = await self._context.new_page()
                for startup_page in startup_pages:
                    try:
                        await startup_page.close()
                    except Exception:
                        pass
                self._ready = True
                self._init_error = None
                logger.info(
                    "browser pool started: data_dir=%s headless=%s channel=%s executable=%s proxy_configured=%s",
                    _USER_DATA_DIR,
                    _HEADLESS,
                    _BROWSER_CHANNEL or "default",
                    _BROWSER_EXECUTABLE_PATH or "default",
                    bool(_EFFECTIVE_BROWSER_PROXY),
                )
            except Exception as e:
                self._init_error = str(e)
                logger.exception("browser pool start failed: %s", e)
                raise

    async def stop(self) -> None:
        async with self._lock:
            for p in self._pages.values():
                try:
                    await p.close()
                except Exception:
                    pass
            self._pages.clear()
            self._xhs_active_page = None
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
            self._context = None
            self._browser = None
            self._keepalive_page = None
            self._pw = None
            self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def init_error(self) -> Optional[str]:
        return self._init_error

    async def _ensure_started(self) -> None:
        if not self._ready:
            await self.start()

    async def _new_platform_page(self, platform: str) -> Page:
        page = await self._context.new_page()  # type: ignore[union-attr]
        try:
            await page.evaluate("name => { window.name = name; return true; }", f"mavis-scraper-{platform}")
        except Exception:
            pass
        return page

    async def _get_page(self, platform: str) -> Page:
        """懒加载一个平台专属 page；小红书详情 popup 可暂时成为活动页。"""
        await self._ensure_started()
        if platform == "xhs" and self._xhs_active_page is not None:
            if not self._xhs_active_page.is_closed():
                return self._xhs_active_page
            self._xhs_active_page = None
        if platform in self._pages:
            page = self._pages[platform]
            if not page.is_closed():
                return page
        # Always allocate a clean platform tab. The initial page restored by
        # launch_persistent_context can be a stuck about:blank/WebUI surface;
        # reusing it caused every real navigation to time out. A new page still
        # shares the same persistent cookies and storage without deleting login data.
        page = await self._new_platform_page(platform)
        self._pages[platform] = page
        return page

    async def _get_xhs_profile_page(self) -> Page:
        """Return the persistent XHS profile tab, never a temporary detail popup."""
        await self._ensure_started()
        page = self._pages.get("xhs")
        if page is None or page.is_closed():
            page = await self._get_page("xhs")
            self._pages["xhs"] = page
        return page

    async def restore_xhs_profile_page(self) -> None:
        """Restore the persistent profile tab after popup or same-tab detail use."""
        popup = self._xhs_active_page
        self._xhs_active_page = None
        profile_page = self._pages.get("xhs")
        if popup is not None and popup is not profile_page:
            try:
                if not popup.is_closed():
                    await popup.close()
            except Exception as exc:
                logger.debug("xhs detail popup close failed: %s", type(exc).__name__)
            return
        if profile_page is None or profile_page.is_closed():
            return
        path = urlsplit(profile_page.url or "").path
        if "/explore/" not in path and "/discovery/item/" not in path:
            return
        try:
            await profile_page.go_back(wait_until="domcontentloaded", timeout=15000)
            logger.info("xhs same-tab detail returned through browser history")
        except Exception as exc:
            logger.warning("xhs same-tab detail history return failed: %s", type(exc).__name__)

    async def navigate(self, platform: str, url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 20000) -> None:
        """导航到 URL。"""
        page = await self._get_page(platform)
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as e:
            raise ScraperError(
                f"browser navigate failed: {_safe_url_for_error(url)} ({e})",
                code=ErrorCode.UPSTREAM_TIMEOUT,
                status_code=504,
                retryable=True,
                capability="browser-navigate",
            )

    async def current_url(self, platform: str) -> str:
        """Return the current page URL for control flow; callers must not log secrets."""
        page = await self._get_page(platform)
        return page.url or ""

    async def back(self, platform: str, *, timeout_ms: int = 15000) -> bool:
        """Go back within the platform tab and report whether navigation occurred."""
        page = await self._get_page(platform)
        try:
            response = await page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
            return response is not None or bool(page.url)
        except Exception:
            return False

    async def click_xhs_note_card(
        self,
        note_id: str,
        *,
        timeout_ms: int = 5000,
        capture_popup: bool = False,
    ) -> bool:
        """Click a visible XHS discovery card with Playwright native input.

        ``capture_popup`` is used only by account imports. It observes the
        browser-native popup created by the real click and makes that Page the
        temporary active XHS page. It never opens a URL or clicks through JS.
        """
        page = await self._get_xhs_profile_page()
        target = str(note_id or "")
        if not target or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target):
            return False
        anchors = page.locator('a[href*="/explore/"], a[href*="/discovery/item/"]')
        matched_count = 0
        visible_count = 0
        try:
            count = await anchors.count()
            for index in range(count):
                anchor = anchors.nth(index)
                href = await anchor.get_attribute("href")
                path = urlsplit(str(href or "")).path
                match = re.search(r"/(?:explore|discovery/item)/([^/?#]+)", path)
                if not match or match.group(1) != target:
                    continue
                matched_count += 1
                click_target = None
                box = None
                candidates = [anchor]
                media = anchor.locator("img, video, picture").first
                card = anchor.locator(
                    "xpath=ancestor::*[self::section or contains(concat(' ', normalize-space(@class), ' '), ' note-item ')][1]"
                ).first
                candidates.extend([media, card])
                for candidate in candidates:
                    try:
                        if not await candidate.is_visible():
                            continue
                        await candidate.scroll_into_view_if_needed(timeout=timeout_ms)
                        candidate_box = await candidate.bounding_box()
                    except Exception:
                        continue
                    if not candidate_box or candidate_box.get("width", 0) <= 0 or candidate_box.get("height", 0) <= 0:
                        continue
                    click_target = candidate
                    box = candidate_box
                    break
                if click_target is None or box is None:
                    continue
                visible_count += 1
                click_x = box["x"] + box["width"] / 2
                click_y = box["y"] + box["height"] / 2
                if not capture_popup:
                    await page.mouse.click(click_x, click_y)
                    logger.info("xhs native href-card click dispatched: note=%s popup_watch=false", target)
                    return True
                popup = None
                try:
                    async with page.expect_popup(timeout=min(timeout_ms, 3000)) as popup_info:
                        await page.mouse.click(click_x, click_y)
                    popup = await popup_info.value
                except PlaywrightTimeoutError:
                    # A same-tab navigation is valid; the click has already
                    # happened. Do not retry it just because no popup appeared.
                    pass
                if popup is not None and not popup.is_closed():
                    try:
                        await popup.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 10000))
                    except Exception:
                        # The detail state waiter below handles pages that are
                        # still loading or where the lifecycle was interrupted.
                        pass
                    self._xhs_active_page = popup
                logger.info(
                    "xhs native href-card click dispatched: note=%s popup_watch=true popup_captured=%s",
                    target,
                    popup is not None,
                )
                return True
            logger.info(
                "xhs native href-card click unavailable: note=%s anchors=%s matched=%s visible=%s",
                target,
                count,
                matched_count,
                visible_count,
            )
        except Exception as exc:
            logger.warning("xhs native href-card click failed: note=%s error=%s", target, type(exc).__name__)
        return False

    async def evaluate(self, platform: str, js: str, arg: Any = None, *, timeout_ms: int = 15000) -> Any:
        """在 platform page 上执行 JS，返回 raw value。
        如果传 arg，会作为 JS 的第一个参数（Playwright 自动 JSON 序列化）。"""
        page = await self._get_page(platform)
        try:
            if arg is not None:
                return await asyncio.wait_for(page.evaluate(js, arg), timeout=timeout_ms / 1000)
            return await asyncio.wait_for(page.evaluate(js), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            raise ScraperError(
                f"browser evaluate timeout: {js[:80]!r}",
                code=ErrorCode.UPSTREAM_TIMEOUT,
                status_code=504,
                retryable=True,
                capability="browser-evaluate",
            )
        except Exception as e:
            raise ScraperError(
                f"browser evaluate failed: {e}",
                code=ErrorCode.SCRAPE_ERROR,
                status_code=500,
                retryable=True,
                capability="browser-evaluate",
            )

    async def wait_for_state(self, platform: str, js: str, *, timeout_ms: int = 15000) -> Any:
        """轮询 JS 表达式直到返回 truthy。"""
        page = await self._get_page(platform)
        try:
            return await page.wait_for_function(js, timeout=timeout_ms)
        except Exception as e:
            raise ScraperError(
                f"browser wait_for_state timeout: {js[:80]!r} ({e})",
                code=ErrorCode.UPSTREAM_TIMEOUT,
                status_code=504,
                retryable=True,
                capability="browser-wait",
            )

    async def screenshot(self, platform: str) -> bytes:
        """截 platform 当前页面的 PNG 字节。"""
        page = await self._get_page(platform)
        return await page.screenshot(type="png")

    async def fetch(self, platform: str, url: str, *, headers: Optional[dict] = None, timeout_ms: int = 15000) -> tuple[int, str]:
        """用 page context 的 cookie 模拟浏览器 fetch。
        Returns (status_code, body_text)."""
        page = await self._get_page(platform)
        try:
            r = await page.request.get(url, headers=headers or {}, timeout=timeout_ms)
            return r.status, await r.text()
        except Exception as e:
            raise ScraperError(
                f"page.request.get failed: {e}",
                code=ErrorCode.SCRAPE_ERROR,
                status_code=500,
                retryable=True,
                capability="browser-fetch",
            )

    async def get_cookies(self, platform: str) -> list[dict]:
        """拿 platform 对应域的 cookie。"""
        await self._ensure_started()
        home = PLATFORM_HOME.get(platform, platform)
        try:
            return await self._context.cookies(f"https://{home}")  # type: ignore[union-attr]
        except Exception as e:
            logger.warning("get_cookies failed for %s: %s", platform, e)
            return []

    def is_logged_in(self, platform: str, cookies: list[dict]) -> bool:
        """启发式判断是否已登录（看是否有常见登录态 cookie）。"""
        if not cookies:
            return False
        keys = {c.get("name", "") for c in cookies}
        markers = {
            "xhs": {"web_session"},
            "douyin": {"sessionid", "sessionid_ss"},
            "kuaishou": {"userId", "kuaishou.web.cp.api_st"},
            "tiktok": {"sessionid"},
            "bilibili": {"DedeUserID", "SESSDATA"},
            "twitter": {"auth_token", "ct0"},
            "youtube": {"SID", "HSID", "LOGIN_INFO"},
        }
        for k in markers.get(platform, set()):
            if k in keys:
                return True
        return False

    async def status(self) -> dict:
        """返回浏览器状态摘要。"""
        platforms = list(LOGIN_URLS.keys())
        logged: dict[str, bool] = {}
        for p in platforms:
            try:
                cookies = await self.get_cookies(p)
                logged[p] = self.is_logged_in(p, cookies)
            except Exception:
                logged[p] = False
        current_paths: dict[str, str] = {}
        page_diagnostics: dict[str, dict] = {}
        diagnostic_js = r"""
        () => {
          const text = document.body ? (document.body.innerText || '') : '';
          const rawMap = window.__INITIAL_STATE__?.note?.noteDetailMap;
          const map = rawMap?.value !== undefined ? rawMap.value : rawMap?._value !== undefined ? rawMap._value : rawMap;
          return {
            ready_state: document.readyState,
            body_text_length: text.length,
            has_initial_state: !!window.__INITIAL_STATE__,
            note_detail_count: map && typeof map === 'object' ? Object.keys(map).length : 0,
            selectors: {
              detail_title: document.querySelectorAll('#detail-title').length,
              detail_desc: document.querySelectorAll('#detail-desc').length,
              note_content: document.querySelectorAll('.note-content').length,
              note_scroller: document.querySelectorAll('.note-scroller').length,
              interaction: document.querySelectorAll('.interaction-container').length,
              slider_images: document.querySelectorAll('.note-slider-img img').length,
              videos: document.querySelectorAll('video').length,
              iframes: document.querySelectorAll('iframe').length,
            },
            flags: {
              login: /登录|扫码登录/.test(text),
              verification: /扫码查看|打开小红书App扫码|安全验证|访问频繁/.test(text),
              inaccessible: /当前笔记暂时无法浏览|内容不存在|笔记不存在|已被删除|私密笔记|仅作者可见/.test(text),
            },
          };
        }
        """
        for platform, page in self._pages.items():
            if page.is_closed():
                continue
            try:
                current_paths[platform] = urlsplit(page.url or "").path or "/"
            except Exception:
                current_paths[platform] = ""
            try:
                diagnostic = await asyncio.wait_for(page.evaluate(diagnostic_js), timeout=3)
                if isinstance(diagnostic, dict):
                    page_diagnostics[platform] = diagnostic
            except Exception:
                page_diagnostics[platform] = {"unavailable": True}
        bridge_status = {"enabled": self._xhs_bridge is not None, "extension_connected": False}
        if self._xhs_bridge is not None:
            try:
                bridge_result = await asyncio.wait_for(self._xhs_bridge.ping(), timeout=2.5)
                bridge_status["extension_connected"] = bool(bridge_result.get("extension_connected"))
            except (SafeBridgeError, asyncio.TimeoutError):
                pass
        return {
            "ready": self._ready,
            "headless": _HEADLESS,
            "user_data_dir": str(_USER_DATA_DIR),
            "init_error": self._init_error,
            "logged_in": logged,
            "current_paths": current_paths,
            "page_diagnostics": page_diagnostics,
            "proxy_configured": bool(_EFFECTIVE_BROWSER_PROXY),
            "xhs_safe_bridge": bridge_status,
        }


# 全局单例
pool = BrowserPool()
