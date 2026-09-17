"""浏览器管理 API 路由。

- GET  /api/browser/status             - 浏览器启动状态 + 各平台登录态
- POST /api/browser/login/{plat}       - 打开 platform 登录页（headless=False 模式下用户在浏览器里登录）
- POST /api/browser/inject-cookie      - 直接注入 cookie（适合有现成 cookie 的人）
- POST /api/browser/reset              - 清空 user data dir 重新开始
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..errors import ErrorCode, ScraperError
from .pool import LOGIN_URLS, PLATFORM_HOME, _USER_DATA_DIR, pool

logger = logging.getLogger("scraper.browser.api")

router = APIRouter(prefix="/api/browser", tags=["browser"])
_BROWSER_ADMIN_ENABLED = os.environ.get("ENABLE_BROWSER_ADMIN", "0").strip().lower() in {"1", "true", "yes", "on"}


def _require_browser_admin() -> None:
    if _BROWSER_ADMIN_ENABLED:
        return
    raise ScraperError(
        "Browser administration endpoints are disabled in formal mode",
        code=ErrorCode.SERVICE_UNAUTHORIZED,
        status_code=403,
        retryable=False,
        capability="browser-admin",
    )


class InjectCookieBody(BaseModel):
    platform: str
    cookie: str  # 形如 "key1=value1; key2=value2"
    expires_days: int = 30  # cookie 过期时间（Playwright 要求 explicit）


def _parse_cookie_string(cookie_str: str) -> list[dict]:
    """把 'k1=v1; k2=v2' 拆成 playwright 的 cookie 列表。"""
    out: list[dict] = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        out.append({"name": name.strip(), "value": value.strip()})
    return out


@router.get("/status")
async def status() -> dict:
    """返回浏览器启动状态 + 各平台登录态。"""
    try:
        s = await pool.status()
        return {"ok": True, **s}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/login/{platform}")
async def open_login(platform: str) -> dict:
    """为 platform 打开登录页（让用户手动登录）。"""
    if platform not in LOGIN_URLS:
        return {"ok": False, "error": f"unknown platform: {platform}"}
    try:
        await pool.start()
    except Exception as e:
        return {"ok": False, "error": f"browser start failed: {e}"}
    url = LOGIN_URLS[platform]
    try:
        # 抖音/小红书有时会返回非 200 的中间响应（防爬），用 domcontentloaded 而不是 load 更稳
        await pool.navigate(platform, url, wait_until="domcontentloaded", timeout_ms=20000)
        cookies = await pool.get_cookies(platform)
        logged = pool.is_logged_in(platform, cookies)
        return {
            "ok": True,
            "platform": platform,
            "opened_url": url,
            "logged_in": logged,
            "cookie_count": len(cookies),
            "message": "页面已打开，请在浏览器中完成登录" if not logged else "已检测到登录态",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/inject-cookie")
async def inject_cookie(body: InjectCookieBody) -> dict:
    """直接注入 cookie（仅在显式启用浏览器管理接口时可用）。"""
    _require_browser_admin()
    if body.platform not in LOGIN_URLS:
        return {"ok": False, "error": f"unknown platform: {body.platform}"}
    try:
        await pool.start()
    except Exception as e:
        return {"ok": False, "error": f"browser start failed: {e}"}
    home = PLATFORM_HOME.get(body.platform, body.platform)
    parsed = _parse_cookie_string(body.cookie)
    if not parsed:
        return {"ok": False, "error": "no valid cookies parsed"}
    expires = int((datetime.utcnow() + timedelta(days=body.expires_days)).timestamp())
    for c in parsed:
        c["domain"] = f".{home}"
        c["path"] = "/"
        c["expires"] = expires
        c["httpOnly"] = False
        c["secure"] = True
        c["sameSite"] = "Lax"
    try:
        await pool._context.add_cookies(parsed)  # type: ignore[union-attr]
    except Exception as e:
        return {"ok": False, "error": f"add_cookies failed: {e}"}
    cookies = await pool.get_cookies(body.platform)
    logged = pool.is_logged_in(body.platform, cookies)
    return {
        "ok": True,
        "platform": body.platform,
        "injected": len(parsed),
        "logged_in": logged,
        "cookie_count": len(cookies),
    }


@router.get("/screenshot/{platform}")
async def screenshot(platform: str):
    """截 platform 当前页面的 PNG（仅调试模式）。"""
    _require_browser_admin()
    if platform not in LOGIN_URLS:
        return {"ok": False, "error": f"unknown platform: {platform}"}
    try:
        await pool.start()
        data = await pool.screenshot(platform)
        from fastapi.responses import Response
        return Response(content=data, media_type="image/png")
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/eval/{platform}")
async def evaluate_on_platform(platform: str, body: dict) -> dict:
    """在 platform 当前 page 上执行 JS（仅调试模式）。"""
    _require_browser_admin()
    if platform not in LOGIN_URLS:
        return {"ok": False, "error": f"unknown platform: {platform}"}
    js = body.get("expression") or body.get("js") or ""
    if not js:
        return {"ok": False, "error": "missing 'expression' in body"}
    arg = body.get("arg")
    try:
        await pool.start()
        result = await pool.evaluate(platform, js, arg=arg, timeout_ms=int(body.get("timeout_ms", 10000)))
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/goto/{platform}")
async def goto(platform: str, body: dict) -> dict:
    """让 platform 跳转到指定 URL（仅调试模式）。"""
    _require_browser_admin()
    if platform not in LOGIN_URLS:
        return {"ok": False, "error": f"unknown platform: {platform}"}
    url = body.get("url", "")
    if not url:
        return {"ok": False, "error": "missing 'url'"}
    try:
        await pool.start()
        await pool.navigate(platform, url, wait_until=body.get("wait_until", "domcontentloaded"), timeout_ms=int(body.get("timeout_ms", 20000)))
        return {"ok": True, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/click/{platform}")
async def click_text(platform: str, body: dict) -> dict:
    """在 platform page 点击文本（仅调试模式）。"""
    _require_browser_admin()
    if platform not in LOGIN_URLS:
        return {"ok": False, "error": f"unknown platform: {platform}"}
    text = body.get("text", "")
    if not text:
        return {"ok": False, "error": "missing 'text'"}
    js = f"""
() => {{
  const target = {json.dumps(text)};
  const all = Array.from(document.querySelectorAll('*'));
  for (const el of all) {{
    const t = (el.textContent || '').trim();
    if (t === target || t.includes(target)) {{
      // 找最近的 clickable 祖先
      let clickable = el;
      for (let i = 0; i < 5; i++) {{
        if (!clickable) break;
        const tag = clickable.tagName;
        if (['BUTTON', 'A'].includes(tag) || clickable.onclick || clickable.getAttribute('role') === 'button') break;
        clickable = clickable.parentElement;
      }}
      try {{
        clickable.click();
        return {{clicked: true, tag: clickable.tagName, cls: (clickable.className||'').slice(0, 60)}};
      }} catch (e) {{
        return {{clicked: false, error: e.message}};
      }}
    }}
  }}
  return {{clicked: false, error: 'text not found'}};
}}
"""
    try:
        await pool.start()
        r = await pool.evaluate(platform, js, timeout_ms=10000)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/reset")
async def reset() -> dict:
    """清空浏览器数据目录（仅调试模式）。"""
    _require_browser_admin()
    await pool.stop()
    if _USER_DATA_DIR.exists():
        try:
            shutil.rmtree(_USER_DATA_DIR)
        except Exception as e:
            return {"ok": False, "error": f"rmtree failed: {e}"}
    return {"ok": True, "user_data_dir": str(_USER_DATA_DIR), "message": "已清空浏览器数据目录"}

