"""正式版凭证辅助接口。

- POST /api/credentials/{platform}/browser

凭证只从当前 Playwright 持久浏览器上下文读取，并保存在 scraper 进程内
供真实抓取器使用；不生成、不返回 mock Cookie。B 站二维码接口已停用，
正式登录统一使用 /api/browser/login/bilibili 后由用户在浏览器中完成登录。
"""

from __future__ import annotations

from fastapi import APIRouter, Path

from ..errors import ErrorCode, ScraperError
from ..scrapers.cookies import set_cookie
from ..browser.pool import LOGIN_URLS, pool

router = APIRouter()


# ---- 7.1 浏览器 Cookie 提取 ----
@router.post("/api/credentials/{platform}/browser")
async def extract_browser_cookie(platform: str = Path(...)) -> dict:
    """从真实浏览器登录态提取 Cookie，并仅在 scraper 进程内保存。"""
    if platform not in LOGIN_URLS:
        raise ScraperError(
            f"Platform {platform} not supported for browser cookie extraction",
            code=ErrorCode.INVALID_PLATFORM,
            status_code=400,
            retryable=False,
            capability="credentials",
        )

    try:
        await pool.start()
        cookies = await pool.get_cookies(platform)
    except Exception as exc:
        raise ScraperError(
            f"Unable to read browser cookies for {platform}: {exc}",
            code=ErrorCode.SCRAPE_ERROR,
            status_code=503,
            retryable=True,
            capability="credentials",
        ) from exc

    if not pool.is_logged_in(platform, cookies):
        raise ScraperError(
            f"No active login session for {platform}. Open /api/browser/login/{platform} "
            "and complete login in the browser first.",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="credentials",
        )

    cookie_header = "; ".join(
        f"{item.get('name')}={item.get('value')}"
        for item in cookies
        if item.get("name") and item.get("value") is not None
    )
    if not cookie_header:
        raise ScraperError(
            f"Browser returned no usable cookies for {platform}",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="credentials",
        )
    set_cookie(platform, cookie_header)
    return {
        "platform": platform,
        "success": True,
        "logged_in": True,
        "cookie_count": len(cookies),
        "message": "已从真实浏览器登录态读取凭证，Cookie 仅保存在 scraper 进程内",
    }


# ---- 7.2 旧 B 站二维码接口 ----
@router.post("/api/credentials/bilibili/qr/start")
async def start_bilibili_qr() -> dict:
    raise ScraperError(
        "二维码登录接口已停用。请使用 /api/browser/login/bilibili，"
        "并在打开的真实浏览器窗口中完成登录。",
        code=ErrorCode.SCRAPE_ERROR,
        status_code=501,
        retryable=False,
        capability="credentials",
    )


@router.get("/api/credentials/bilibili/qr/status")
async def get_bilibili_qr_status() -> dict:
    raise ScraperError(
        "二维码登录接口已停用，请改用真实浏览器登录流程。",
        code=ErrorCode.SCRAPE_ERROR,
        status_code=501,
        retryable=False,
        capability="credentials",
    )
