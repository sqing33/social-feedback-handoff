"""小红书抓取器（持久浏览器页面 + __INITIAL_STATE__ 提取）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
import unicodedata
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .base import BaseScraper
from ..browser.pool import pool
from ..errors import ErrorCode, ScraperError
from ..xhs_bridge import SafeBridgeError, SafeXhsBridgeClient
from ..xhs_bridge.client import bridge_from_environment

logger = logging.getLogger("scraper.xhs")

_PROFILE_URL = "https://www.xiaohongshu.com/user/profile/{user_id}"
_DETAIL_URL = "https://www.xiaohongshu.com/explore/{note_id}"
_USE_EXISTING_CHROME_TAB = os.environ.get("XHS_USE_EXISTING_CHROME_TAB", "0").lower() in {"1", "true", "yes", "on"}
_SAFE_BRIDGE: SafeXhsBridgeClient | None = bridge_from_environment()

_EXISTING_CHROME_TAB_SCRIPT = r'''
on run argv
  set noteId to item 1 of argv
  set jsCode to item 2 of argv
  tell application "Google Chrome"
    repeat with windowIndex from 1 to count of windows
      repeat with tabIndex from 1 to count of tabs of window windowIndex
        set tabUrl to URL of tab tabIndex of window windowIndex
        if tabUrl contains ("/explore/" & noteId) then
          tell tab tabIndex of window windowIndex to return execute javascript jsCode
        end if
      end repeat
    end repeat
  end tell
  return ""
end run
'''


async def _evaluate_existing_chrome_tab(note_id: str, javascript: str) -> Any:
    """Read an already-open matching Chrome tab without inspecting cookies or navigating it."""
    def run() -> str:
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", "-e", _EXISTING_CHROME_TAB_SCRIPT, note_id, javascript],
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            logger.warning("existing Chrome tab evaluation timed out for note %s", note_id)
            return ""
        if completed.returncode != 0:
            logger.warning("existing Chrome tab evaluation failed for note %s: %s", note_id, completed.stderr.strip())
            return ""
        return completed.stdout.strip()

    raw = await asyncio.to_thread(run)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("existing Chrome tab returned invalid JSON for note %s", note_id)
        return None


async def _extract_existing_chrome_detail(note_id: str) -> dict | None:
    expression = f"""
JSON.stringify((() => {{
  const textOf = selector => {{
    const element = document.querySelector(selector);
    return element ? (element.innerText || element.textContent || '').trim() : '';
  }};
  const parseCount = raw => {{
    const text = String(raw ?? '').replace(/,/g, '').trim();
    const match = text.match(/([\\d.]+)\\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    return Math.round(Number.parseFloat(match[1]) * factor);
  }};
  const images = Array.from(document.querySelectorAll('.note-slider-img img'))
    .map(image => image.currentSrc || image.src || image.getAttribute('data-src') || '')
    .filter(Boolean);
  const video = document.querySelector('video');
  const title = textOf('.note-content .title, #detail-title') || document.title.replace(/\\s+-\\s+小红书$/, '').trim();
  const author = textOf('.author-container .name, .author-container .user-name, .author .name, .user-name');
  const isVideo = !!video && !images.length;
  return {{
    note_id: {json.dumps(note_id)},
    title,
    description: textOf('.note-content .desc, .note-text'),
    user: {{nickname: author}},
    cover: images[0] || (video && video.poster) || '',
    images: isVideo ? [] : images,
    media_type: isVideo ? 'video' : 'image',
    publish_time: null,
    view_count: null,
    play_count: null,
    liked_count: parseCount(textOf('.buttons.engage-bar-style .like-wrapper .count')),
    collected_count: parseCount(textOf('.buttons.engage-bar-style .collect-wrapper .count')),
    comment_count: parseCount(textOf('.buttons.engage-bar-style .chat-wrapper .count')) || parseCount(textOf('.comments-container .total')),
    share_count: null,
    url: 'https://www.xiaohongshu.com/explore/' + {json.dumps(note_id)},
  }};
}})())
"""
    value = await _evaluate_existing_chrome_tab(note_id, expression)
    return value if isinstance(value, dict) else None


async def _extract_existing_chrome_comments(note_id: str) -> list[dict] | None:
    await _evaluate_existing_chrome_tab(
        note_id,
        "JSON.stringify((() => { window.scrollTo(0, document.body.scrollHeight); return true; })())",
    )
    await asyncio.sleep(1.5)
    expression = f"JSON.stringify(({_EXTRACT_COMMENTS_JS})())"
    value = await _evaluate_existing_chrome_tab(note_id, expression)
    return value if isinstance(value, list) else None


def _normalize_account_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r"^@", "", value).strip()
    return re.sub(r"\s+", " ", value).casefold()


def _names_equal(left: str, right: str) -> bool:
    expected = _normalize_account_name(right)
    return bool(expected) and _normalize_account_name(left) == expected


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace(",", "").strip()
    match = re.search(r"([\d.]+)\s*([万亿wk]?)", text, re.I)
    if not match:
        return None
    factor = {"万": 10_000, "亿": 100_000_000, "w": 10_000, "k": 1_000}.get(match.group(2).lower(), 1)
    return int(float(match.group(1)) * factor)


def _profile_from_identifier(identifier: str) -> tuple[str, str]:
    raw = str(identifier or "").strip()
    if not raw:
        return "", ""
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        if not parsed.hostname or not parsed.hostname.lower().endswith("xiaohongshu.com"):
            return "", ""
        match = re.search(r"/user/profile/([^/?#]+)", parsed.path)
        if not match:
            return "", ""
        user_id = unquote(match.group(1))
        query = parse_qs(parsed.query)
        params: list[tuple[str, str]] = []
        for key in ("xsec_token", "xsec_source"):
            if query.get(key):
                params.append((key, query[key][0]))
        url = _PROFILE_URL.format(user_id=quote(user_id, safe=""))
        return user_id, f"{url}?{urlencode(params)}" if params else url
    return raw, _PROFILE_URL.format(user_id=quote(raw, safe=""))


def _parse_note_identifier(identifier: str) -> tuple[str, str, str]:
    """Accept raw note IDs, ``noteId?...`` composites, and full detail URLs."""
    raw = str(identifier or "").strip()
    if not raw:
        return "", "", ""
    token = ""
    source = ""
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        match = re.search(r"/(?:explore|discovery/item)/([^/?#]+)", parsed.path)
        note_id = unquote(match.group(1)) if match else ""
        query = parse_qs(parsed.query)
    else:
        note_part, _, query_part = raw.partition("?")
        note_id = unquote(note_part.rstrip("/").rsplit("/", 1)[-1])
        query = parse_qs(query_part)
    if query.get("xsec_token"):
        token = query["xsec_token"][0]
    if query.get("xsec_source"):
        source = query["xsec_source"][0]
    return note_id, token, source


def _detail_url(note_id: str, token: str = "", source: str = "") -> str:
    base = _DETAIL_URL.format(note_id=quote(note_id, safe=""))
    params: list[tuple[str, str]] = []
    if token:
        params.append(("xsec_token", token))
    if source:
        params.append(("xsec_source", source))
    return f"{base}?{urlencode(params)}" if params else base


_SEARCH_WAIT_JS = r"""
() => {
  const state = window.__INITIAL_STATE__;
  if (state && state.search && state.search.feeds) {
    const feeds = state.search.feeds.value !== undefined ? state.search.feeds.value : state.search.feeds._value;
    if (Array.isArray(feeds) && feeds.length > 0) return true;
  }
  return document.querySelectorAll('section.note-item a[href*="/explore/"], section.note-item a[href*="/discovery/item/"]').length > 0;
}
"""

_DETAIL_WAIT_JS = r"""
() => {
  const state = window.__INITIAL_STATE__;
  if (state && state.note && state.note.noteDetailMap) {
    const raw = state.note.noteDetailMap;
    const map = raw.value !== undefined ? raw.value : raw._value !== undefined ? raw._value : raw;
    if (map && Object.keys(map).length > 0) return true;
  }
  return !!document.querySelector(
    '.note-content .title, #detail-title, .buttons.engage-bar-style, .note-slider-img img, video'
  );
}
"""

_PROFILE_WAIT_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]'));
  return anchors.some(anchor => {
    const card = anchor.closest('section.note-item, .note-item') || anchor;
    if (!card || typeof card.getBoundingClientRect !== 'function') return false;
    const rect = card.getBoundingClientRect();
    const style = getComputedStyle(card);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
      && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
  });
}
"""

_COMMENT_WAIT_JS = r"""
() => {
  const state = window.__INITIAL_STATE__;
  if (state && state.comment && state.comment.comments) {
    const comments = state.comment.comments.value !== undefined
      ? state.comment.comments.value : state.comment.comments._value;
    if (comments && Array.isArray(comments.comments) && comments.comments.length > 0) return true;
  }
  return document.querySelectorAll('.comment-item').length > 0;
}
"""

_AUTH_STATE_JS = r"""
() => {
  const text = document.body ? document.body.innerText || '' : '';
  return /\/login(?:\/|$)/.test(location.pathname)
    || (!!document.querySelector('.login-container, [class*="login-modal"]') && /登录|扫码/.test(text));
}
"""

_CLICK_DETAIL_CARD_JS = r"""
(targetNoteId) => {
  const target = String(targetNoteId || '');
  const anchors = Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]'));
  const anchor = anchors.find(item => {
    try {
      const path = new URL(item.href || item.getAttribute('href') || '', location.href).pathname;
      return path.includes(`/explore/${target}`) || path.includes(`/discovery/item/${target}`);
    } catch (_) {
      return false;
    }
  });
  if (!anchor) return false;
  anchor.removeAttribute('target');
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
    anchor.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
  }
  anchor.click();
  return true;
}
"""

_PAGE_STATE_JS = r"""
(targetNoteId) => {
  const target = String(targetNoteId || '');
  const path = location.pathname || '';
  const text = document.body ? (document.body.innerText || '') : '';
  const rawMap = window.__INITIAL_STATE__?.note?.noteDetailMap;
  const map = rawMap?.value !== undefined ? rawMap.value : rawMap?._value !== undefined ? rawMap._value : rawMap;
  const stateReady = !!(map && typeof map === 'object' && (
    map[target] || Object.entries(map).some(([key, value]) =>
      key.includes(target) || String(value?.note?.noteId || value?.note?.id || '') === target
    )
  ));
  const domReady = !!document.querySelector(
    '#detail-title, #detail-desc, .note-content .title, .note-content .desc, .buttons.engage-bar-style, .note-slider-img img, video'
  );
  return {
    path,
    target_match: path.includes(`/explore/${target}`) || path.includes(`/discovery/item/${target}`),
    detail_ready: stateReady || domReady,
    login_wall: /\/login(?:\/|$)/.test(path)
      || (!!document.querySelector('.login-container, [class*="login-modal"]') && /登录|扫码/.test(text)),
    verification_wall: /扫码查看|打开小红书App扫码|请使用小红书App扫码|安全验证|访问频繁/.test(text),
    inaccessible: /当前笔记暂时无法浏览|内容不存在|笔记不存在|该笔记已被删除|私密笔记|仅作者可见|因用户设置，你无法查看|因违规无法查看/.test(text),
  };
}
"""


async def _navigate_marked_chrome_tab(url: str) -> bool:
    """Use Chrome's native navigation for the marked scraper tab when authorized."""
    if sys.platform != "darwin":
        return False
    escaped_url = str(url or "").replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
set targetUrl to "{escaped_url}"
tell application "Google Chrome"
  repeat with w in windows
    repeat with tb in tabs of w
      try
        set marker to execute tb javascript "window.name"
        if marker is "mavis-scraper-xhs" then
          set URL of tb to targetUrl
          return "OK"
        end if
      end try
    end repeat
  end repeat
end tell
return "NOT_FOUND"
'''

    def run() -> bool:
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript"],
                input=script,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            return False
        return completed.returncode == 0 and completed.stdout.strip() == "OK"

    return await asyncio.to_thread(run)


async def _navigate_xhs_page(
    url: str,
    expected_path_fragment: str,
    timeout_ms: int = 20000,
    *,
    allow_js_fallback: bool = True,
) -> None:
    """Navigate through the marked real Chrome tab, with controlled fallbacks."""
    try:
        await pool.evaluate("xhs", "() => true", timeout_ms=3000)
    except ScraperError:
        pass

    if await _navigate_marked_chrome_tab(url):
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            current_path = urlsplit(await pool.current_url("xhs")).path
            if expected_path_fragment in current_path:
                return
            await asyncio.sleep(0.5)

    try:
        await pool.navigate("xhs", url, wait_until="commit", timeout_ms=timeout_ms)
        return
    except ScraperError as exc:
        if exc.code != ErrorCode.UPSTREAM_TIMEOUT:
            raise
        original_error = exc

    current_path = urlsplit(await pool.current_url("xhs")).path
    if expected_path_fragment in current_path:
        logger.warning("xhs navigation lifecycle timed out after reaching expected path: %s", expected_path_fragment)
        return
    if not allow_js_fallback:
        raise original_error

    try:
        await pool.evaluate(
            "xhs",
            "target => { window.location.assign(target); return true; }",
            url,
            timeout_ms=5000,
        )
    except ScraperError:
        # Context destruction is expected when location.assign commits quickly.
        pass
    await asyncio.sleep(2.0)
    current_path = urlsplit(await pool.current_url("xhs")).path
    if expected_path_fragment not in current_path:
        raise original_error


async def _detail_page_state(note_id: str) -> dict:
    try:
        value = await pool.evaluate("xhs", _PAGE_STATE_JS, note_id, timeout_ms=5000)
    except ScraperError:
        value = {}
    state = value if isinstance(value, dict) else {}
    if _SAFE_BRIDGE is None:
        return state
    try:
        bridge_state = await _SAFE_BRIDGE.get_page_state(note_id)
    except SafeBridgeError as exc:
        logger.info("safe XHS Bridge unavailable; using Playwright state: %s", exc.code)
        return state
    if not isinstance(bridge_state, dict):
        return state
    # The Bridge contributes only fixed page-state flags. Playwright remains the
    # source of public content extraction, so no Bridge payload is persisted.
    merged = dict(state)
    for key in ("path", "target_match", "detail_ready", "login_wall", "verification_wall", "inaccessible"):
        if key not in merged or bridge_state.get(key):
            merged[key] = bridge_state.get(key)
    return merged


async def _wait_for_detail_state(note_id: str, timeout_ms: int = 20000) -> dict:
    try:
        await pool.wait_for_state(
            "xhs",
            f"() => ({_PAGE_STATE_JS})({json.dumps(note_id)}).detail_ready",
            timeout_ms=timeout_ms,
        )
    except ScraperError:
        logger.warning("xhs detail state not ready: %s", note_id)
    return await _detail_page_state(note_id)


async def _open_detail_page(note_id: str, url: str, *, account_import: bool = False) -> dict:
    """Open a note from a live href-matched card, with direct fallback only outside account imports."""
    state = await _detail_page_state(note_id)
    if state.get("target_match") and state.get("detail_ready"):
        return state
    if account_import and any(state.get(key) for key in ("verification_wall", "inaccessible", "login_wall")):
        return state

    path = str(state.get("path") or "")
    if ("/explore/" in path or "/discovery/item/" in path) and not state.get("target_match"):
        if await pool.back("xhs", timeout_ms=15000):
            await asyncio.sleep(random.uniform(5.0, 10.0))
            state = await _detail_page_state(note_id)
            path = str(state.get("path") or "")
            if account_import and any(state.get(key) for key in ("verification_wall", "inaccessible", "login_wall")):
                return state

    if account_import and "/user/profile/" not in path:
        raise ScraperError(
            "xhs account import is not on a visible profile page; direct detail navigation is disabled",
            code=ErrorCode.CONTENT_NOT_FOUND,
            status_code=409,
            retryable=True,
            capability="account-card-navigation",
        )

    click_kwargs = {"timeout_ms": 5000}
    if account_import:
        click_kwargs["capture_popup"] = True
    clicked = await pool.click_xhs_note_card(note_id, **click_kwargs)
    if not clicked and not account_import and _SAFE_BRIDGE is not None:
        try:
            clicked = await _SAFE_BRIDGE.click_note_card(note_id)
        except SafeBridgeError as exc:
            logger.info("safe XHS Bridge card click unavailable; using local page: %s", exc.code)
    if not clicked and not account_import:
        clicked = await pool.evaluate("xhs", _CLICK_DETAIL_CARD_JS, note_id, timeout_ms=5000)
    if clicked:
        state = await _wait_for_detail_state(note_id, timeout_ms=20000)
        if state.get("detail_ready") or state.get("verification_wall") or state.get("inaccessible") or state.get("login_wall"):
            return state

    if account_import:
        raise ScraperError(
            "xhs account import could not open the visible href-matched note card; direct detail navigation is disabled",
            code=ErrorCode.CONTENT_NOT_FOUND,
            status_code=409,
            retryable=True,
            capability="account-card-navigation",
        )

    await _navigate_xhs_page(url, f"/explore/{note_id}", timeout_ms=20000)
    return await _wait_for_detail_state(note_id, timeout_ms=20000)


async def _raise_for_detail_page_state(note_id: str, state: dict, capability: str) -> None:
    if state.get("login_wall"):
        raise ScraperError(
            "xhs browser login is missing or expired",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability=capability,
        )
    if state.get("verification_wall"):
        raise ScraperError(
            "xhs temporarily requires interactive verification; retry after completing it in the persistent browser",
            code=ErrorCode.RATE_LIMITED,
            status_code=429,
            retryable=True,
            capability=capability,
        )
    if state.get("inaccessible"):
        raise ScraperError(
            f"xhs note is not publicly accessible: {note_id}",
            code=ErrorCode.CONTENT_NOT_FOUND,
            status_code=404,
            retryable=False,
            capability=capability,
        )


async def _raise_for_profile_page_state(user_id: str, state: dict) -> None:
    if state.get("login_wall"):
        raise ScraperError(
            "xhs browser login is missing or expired",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="account-contents",
        )
    if state.get("verification_wall"):
        raise ScraperError(
            "xhs temporarily requires interactive verification on the account profile",
            code=ErrorCode.RATE_LIMITED,
            status_code=429,
            retryable=True,
            capability="account-contents",
        )
    if state.get("inaccessible"):
        raise ScraperError(
            f"xhs account profile is not publicly accessible: {user_id}",
            code=ErrorCode.CONTENT_NOT_FOUND,
            status_code=404,
            retryable=False,
            capability="account-contents",
        )


_EXTRACT_SEARCH_JS = r"""
() => {
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    return Math.round(Number.parseFloat(match[1]) * factor);
  };
  const state = window.__INITIAL_STATE__;
  const feeds = state?.search?.feeds;
  const data = feeds?.value !== undefined ? feeds.value : feeds?._value;
  if (Array.isArray(data) && data.length > 0) {
    return data.slice(0, 50).map(feed => {
      const card = feed.noteCard || feed.note || {};
      const user = card.user || feed.user || {};
      const interact = card.interactInfo || feed.interactInfo || {};
      return {
        note_id: String(feed.id || feed.noteId || card.noteId || card.id || ''),
        title: card.displayTitle || card.title || '',
        user: {
          nickname: user.nickname || user.nickName || '',
          user_id: user.userId || user.user_id || user.id || '',
        },
        xsec_token: feed.xsecToken || feed.xsec_token || card.xsecToken || card.xsec_token || user.xsecToken || user.xsec_token || '',
        xsec_source: feed.xsecSource || feed.xsec_source || card.xsecSource || card.xsec_source || 'pc_search',
        cover: (card.cover && (card.cover.urlDefault || card.cover.urlPre || card.cover.url)) || '',
        liked_count: parseCount(interact.likedCount),
        collected_count: parseCount(interact.collectedCount),
        comment_count: parseCount(interact.commentCount),
        share_count: parseCount(interact.sharedCount ?? interact.shareCount),
      };
    }).filter(item => item.note_id);
  }

  return Array.from(document.querySelectorAll('section.note-item')).slice(0, 50).map(section => {
    const anchor = section.querySelector('a[href*="/explore/"], a[href*="/discovery/item/"]');
    if (!anchor) return null;
    let detailUrl;
    try {
      detailUrl = new URL(anchor.href || anchor.getAttribute('href') || '', location.href);
    } catch (_) {
      return null;
    }
    const match = detailUrl.pathname.match(/\/(?:explore|discovery\/item)\/([^/?#]+)/);
    if (!match) return null;
    const title = section.querySelector('a.title, .footer .title');
    const author = section.querySelector('a.author .name, .author .name, .card-bottom-wrapper .name');
    const cover = section.querySelector('a.cover img, .cover img, img');
    const like = section.querySelector('.like-wrapper .count, .like-wrapper');
    return {
      note_id: match[1],
      title: title ? (title.innerText || title.textContent || '').trim() : '',
      user: {nickname: author ? (author.innerText || author.textContent || '').trim() : '', user_id: ''},
      xsec_token: detailUrl.searchParams.get('xsec_token') || '',
      xsec_source: detailUrl.searchParams.get('xsec_source') || 'pc_search',
      cover: cover ? (cover.currentSrc || cover.src || cover.getAttribute('data-src') || '') : '',
      liked_count: parseCount(like ? (like.innerText || like.textContent || '') : ''),
      collected_count: null,
      comment_count: null,
      share_count: null,
    };
  }).filter(Boolean);
}
"""


# user.notes can be an immutable wrapper containing several nested arrays. Walk
# those arrays/objects and retain only objects with note-card semantics.
_EXTRACT_PROFILE_JS = r"""
(targetUserId) => {
  const state = window.__INITIAL_STATE__ || {};
  const userState = state.user || {};
  const unwrap = (value) => {
    let current = value;
    for (let i = 0; i < 4 && current && typeof current === 'object'; i += 1) {
      if (Object.prototype.hasOwnProperty.call(current, 'value')) current = current.value;
      else if (Object.prototype.hasOwnProperty.call(current, '_value')) current = current._value;
      else break;
    }
    return current;
  };
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    return Math.round(Number.parseFloat(match[1]) * factor);
  };
  const pickUrl = (value, depth = 0) => {
    value = unwrap(value);
    if (!value || depth > 4) return '';
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      for (const child of value) {
        const found = pickUrl(child, depth + 1);
        if (found) return found;
      }
      return '';
    }
    if (typeof value === 'object') {
      for (const key of ['urlDefault', 'urlPre', 'url', 'urlList']) {
        if (typeof value[key] === 'string' && value[key]) return value[key];
      }
      for (const child of Object.values(value)) {
        const found = pickUrl(child, depth + 1);
        if (found) return found;
      }
    }
    return '';
  };
  const pageData = unwrap(userState.userPageData) || {};
  const basicInfo = unwrap(pageData.basicInfo || pageData.user || pageData) || {};
  const domProfileAuthor = (() => {
    const element = document.querySelector('.user-name, .user-name-box .user-name, .profile-info .name, .profile-info .nickname');
    return element ? (element.innerText || element.textContent || '').trim() : '';
  })();
  const profileAuthor = basicInfo.nickname || basicInfo.nickName || basicInfo.name || domProfileAuthor || '';
  const profileUserId = String(basicInfo.userId || basicInfo.user_id || basicInfo.id || targetUserId || '');
  const visibleCardFor = (anchor) => {
    const card = anchor.closest('section.note-item, .note-item') || anchor;
    if (!card || typeof card.getBoundingClientRect !== 'function') return card;
    const rect = card.getBoundingClientRect();
    const style = typeof getComputedStyle === 'function'
      ? getComputedStyle(card) : {display: '', visibility: '', opacity: '1'};
    return rect.width > 0 && rect.height > 0 && style.display !== 'none'
      && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0 ? card : null;
  };
  const linkData = new Map();
  const domOrder = [];
  for (const anchor of document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]')) {
    try {
      if (!visibleCardFor(anchor)) continue;
      const url = new URL(anchor.href || anchor.getAttribute('href'), location.href);
      const match = url.pathname.match(/\/(?:explore|discovery\/item)\/([^/?#]+)/);
      if (!match) continue;
      if (!linkData.has(match[1])) domOrder.push(match[1]);
      linkData.set(match[1], {
        token: url.searchParams.get('xsec_token') || '',
        source: url.searchParams.get('xsec_source') || '',
        href: url.href,
      });
    } catch (_) {}
  }

  const candidates = [];
  const seen = new WeakSet();
  const walk = (input, depth = 0) => {
    const value = unwrap(input);
    if (!value || typeof value !== 'object' || depth > 10 || seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      for (const child of value) walk(child, depth + 1);
      return;
    }
    const card = unwrap(value.noteCard || value.note || value.card || value) || {};
    const noteId = String(value.noteId || value.id || card.noteId || card.id || '');
    const looksLikeNote = !!(value.noteCard || value.note || value.card || card.displayTitle
      || card.interactInfo || card.cover || card.imageList || card.xsecToken || card.xsec_token);
    if (noteId && looksLikeNote) candidates.push({raw: value, card, noteId});
    for (const child of Object.values(value)) walk(child, depth + 1);
  };
  walk(userState.notes);

  const output = new Map();
  for (const candidate of candidates) {
    const {raw, card, noteId} = candidate;
    if (output.has(noteId)) continue;
    const user = unwrap(card.user || raw.user) || {};
    const interact = unwrap(card.interactInfo || raw.interactInfo) || {};
    const linked = linkData.get(noteId) || {};
    const token = raw.xsecToken || raw.xsec_token || card.xsecToken || card.xsec_token || user.xsecToken || user.xsec_token || linked.token || '';
    const source = raw.xsecSource || raw.xsec_source || card.xsecSource || card.xsec_source || linked.source || 'pc_user';
    output.set(noteId, {
      note_id: noteId,
      title: card.displayTitle || card.title || raw.displayTitle || raw.title || '',
      author: user.nickname || user.nickName || profileAuthor,
      user_id: String(user.userId || user.user_id || user.id || profileUserId),
      cover: pickUrl(card.cover || card.imageList || raw.cover),
      xsec_token: token,
      xsec_source: source,
      liked_count: parseCount(interact.likedCount),
      collected_count: parseCount(interact.collectedCount),
      comment_count: parseCount(interact.commentCount),
      share_count: parseCount(interact.sharedCount ?? interact.shareCount),
      detail_url: linked.href || '',
    });
  }

  for (const anchor of document.querySelectorAll('a[href*="/explore/"], a[href*="/discovery/item/"]')) {
    if (!visibleCardFor(anchor)) continue;
    let url;
    try {
      url = new URL(anchor.href || anchor.getAttribute('href') || '', location.href);
    } catch (_) {
      continue;
    }
    const match = url.pathname.match(/\/(?:explore|discovery\/item)\/([^/?#]+)/);
    if (!match || output.has(match[1])) continue;
    const noteId = match[1];
    const section = anchor.closest('section.note-item, .note-item') || anchor.parentElement;
    const title = section ? section.querySelector('a.title, .footer .title, .title') : null;
    const author = section ? section.querySelector('.author .name, .card-bottom-wrapper .name, .name') : null;
    const cover = section ? section.querySelector('a.cover img, .cover img, img') : anchor.querySelector('img');
    const like = section ? section.querySelector('.like-wrapper .count, .like-wrapper') : null;
    output.set(noteId, {
      note_id: noteId,
      title: title ? (title.innerText || title.textContent || '').trim() : '',
      author: author ? (author.innerText || author.textContent || '').trim() : profileAuthor,
      user_id: profileUserId,
      cover: cover ? (cover.currentSrc || cover.src || cover.getAttribute('data-src') || '') : '',
      xsec_token: url.searchParams.get('xsec_token') || '',
      xsec_source: url.searchParams.get('xsec_source') || 'pc_user',
      liked_count: parseCount(like ? (like.innerText || like.textContent || '') : ''),
      collected_count: null,
      comment_count: null,
      share_count: null,
      detail_url: url.href,
    });
  }

  return {
    profile_author: profileAuthor,
    profile_user_id: profileUserId,
    items: domOrder.map(noteId => output.get(noteId)).filter(Boolean),
  };
}
"""


_EXTRACT_DETAIL_JS = r"""
(targetNoteId) => {
  const textOf = selector => {
    const element = document.querySelector(selector);
    return element ? (element.innerText || element.textContent || '').trim() : '';
  };
  const parseCount = raw => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    return Math.round(Number.parseFloat(match[1]) * factor);
  };
  const interactiveCount = selector => parseCount(textOf(selector));
  const domFallback = () => {
    const images = Array.from(document.querySelectorAll('.note-slider-img img'))
      .map(image => image.currentSrc || image.src || image.getAttribute('data-src') || '')
      .filter(Boolean);
    const video = document.querySelector('video');
    const posterElement = document.querySelector('.xgplayer-poster');
    const background = posterElement ? (posterElement.style.backgroundImage || getComputedStyle(posterElement).backgroundImage || '') : '';
    const backgroundMatch = background.match(/url\(["']?(.*?)["']?\)/);
    const cover = images[0] || (backgroundMatch ? backgroundMatch[1] : '') || (video && video.poster) || '';
    const commentTotal = parseCount(textOf('.comments-container .total'));
    const title = textOf('#detail-title, .note-content .title, .note-scroller .title, .interaction-container .title') || document.title.replace(/\s+-\s+小红书$/, '').trim();
    const author = textOf('.author-container .name, .author-container .user-name, .author-container .username, .author-wrapper .name, .author-wrapper .username, .author .name');
    const isVideo = !!video && !images.length;
    return {
      note_id: String(targetNoteId || (location.pathname.match(/\/explore\/([^/?#]+)/) || [])[1] || ''),
      title,
      description: textOf('#detail-desc, .note-content .desc, .note-scroller .desc, .interaction-container .desc'),
      user: {nickname: author},
      cover,
      images: isVideo ? [] : images,
      media_type: isVideo ? 'video' : 'image',
      publish_time: null,
      view_count: null,
      play_count: null,
      liked_count: interactiveCount('.buttons.engage-bar-style .like-wrapper .count'),
      collected_count: interactiveCount('.buttons.engage-bar-style .collect-wrapper .count'),
      comment_count: interactiveCount('.buttons.engage-bar-style .chat-wrapper .count') ?? commentTotal,
      share_count: null,
      url: `https://www.xiaohongshu.com/explore/${String(targetNoteId || (location.pathname.match(/\/explore\/([^/?#]+)/) || [])[1] || '')}`,
    };
  };
  const state = window.__INITIAL_STATE__;
  if (!state || !state.note || !state.note.noteDetailMap) return domFallback();
  const unwrap = value => {
    let current = value;
    for (let i = 0; i < 5 && current && typeof current === 'object'; i += 1) {
      if (Object.prototype.hasOwnProperty.call(current, 'value')) current = current.value;
      else if (Object.prototype.hasOwnProperty.call(current, '_value')) current = current._value;
      else break;
    }
    return current;
  };
  const map = unwrap(state.note.noteDetailMap);
  if (!map || typeof map !== 'object') return domFallback();
  const target = String(targetNoteId || '');
  let detail = map[target] || Object.entries(map).find(([key, value]) => {
    const candidate = unwrap(value) || {};
    const candidateNote = unwrap(candidate.note) || {};
    return key.includes(target) || String(candidateNote.noteId || candidateNote.id || '') === target;
  })?.[1] || Object.values(map)[0];
  detail = unwrap(detail);
  if (!detail || !detail.note) return domFallback();
  const note = unwrap(detail.note) || {};
  const pickUrl = (value, depth = 0) => {
    if (!value || depth > 5) return '';
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      for (const child of value) {
        const found = pickUrl(child, depth + 1);
        if (found) return found;
      }
      return '';
    }
    if (typeof value === 'object') {
      for (const key of ['urlDefault', 'urlPre', 'url', 'masterUrl', 'urlList']) {
        if (typeof value[key] === 'string' && value[key]) return value[key];
      }
      for (const child of Object.values(value)) {
        const found = pickUrl(child, depth + 1);
        if (found) return found;
      }
    }
    return '';
  };
  const rawImageList = unwrap(note.imageList);
  const imageList = Array.isArray(rawImageList) ? rawImageList : [];
  const stateImages = imageList.map(item => pickUrl(item)).filter(Boolean);
  const videoValue = unwrap(note.video);
  const video = videoValue && typeof videoValue === 'object' ? videoValue : null;
  const fallback = domFallback();
  const mediaType = video || String(note.type || note.noteType || '').toLowerCase() === 'video'
    ? 'video' : (fallback.media_type || 'image');
  const images = mediaType === 'video' ? [] : (stateImages.length ? stateImages : fallback.images);
  const cover = pickUrl(unwrap(note.cover)) || stateImages[0] || pickUrl(video && video.cover) || fallback.cover || '';
  const interact = unwrap(note.interactInfo) || {};
  const noteUser = unwrap(note.user) || {};
  const fallbackUser = fallback.user || {};
  const noteId = String(note.noteId || note.id || target || fallback.note_id || '');
  return {
    note_id: noteId,
    title: note.title || note.displayTitle || fallback.title || '',
    description: note.desc || note.description || fallback.description || '',
    user: {
      ...noteUser,
      nickname: noteUser.nickname || noteUser.nickName || fallbackUser.nickname || '',
    },
    cover,
    images,
    media_type: mediaType,
    publish_time: parseCount(note.time ?? note.createTime ?? note.publishTime) ?? fallback.publish_time,
    view_count: parseCount(interact.viewCount ?? interact.view_count ?? note.viewCount) ?? fallback.view_count,
    play_count: parseCount(interact.playCount ?? interact.play_count ?? note.playCount) ?? fallback.play_count,
    liked_count: parseCount(interact.likedCount) ?? fallback.liked_count,
    collected_count: parseCount(interact.collectedCount) ?? fallback.collected_count,
    comment_count: parseCount(interact.commentCount) ?? fallback.comment_count,
    share_count: parseCount(interact.sharedCount ?? interact.shareCount) ?? fallback.share_count,
    url: `https://www.xiaohongshu.com/explore/${noteId}`,
  };
}
"""


_EXTRACT_COMMENTS_JS = r"""
() => {
  const state = window.__INITIAL_STATE__;
  if (state && state.comment && state.comment.comments) {
    const data = state.comment.comments.value !== undefined
      ? state.comment.comments.value : state.comment.comments._value;
    if (data && Array.isArray(data.comments) && data.comments.length > 0) {
      return data.comments.slice(0, 30).map(comment => ({
        nickname: (comment.user || {}).nickname || '',
        userId: (comment.user || {}).userId || (comment.user || {}).user_id || '',
        profileUrl: '',
        content: String(comment.content || '').trim(),
        likeCount: comment.likeCount || 0,
        createTime: comment.createTime || 0,
        createTimeText: '',
      })).filter(comment => comment.content);
    }
  }
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text || text === '赞') return 0;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return 0;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    return Math.round(Number.parseFloat(match[1]) * factor);
  };
  return Array.from(document.querySelectorAll('.comment-item')).slice(0, 30).map(comment => {
    const author = comment.querySelector('.author .name, .author-wrapper .name, a.name');
    const content = comment.querySelector('.content .note-text, .content');
    const date = comment.querySelector('.info .date, .date');
    const like = comment.querySelector('.interactions .like .count, .like .count');
    const profileUrl = author ? (author.href || author.getAttribute('href') || '') : '';
    return {
      nickname: author ? (author.innerText || author.textContent || '').trim() : '',
      userId: author ? (author.getAttribute('data-user-id') || '') : '',
      profileUrl,
      content: content ? (content.innerText || content.textContent || '').trim() : '',
      likeCount: parseCount(like ? (like.innerText || like.textContent || '') : ''),
      createTime: 0,
      createTimeText: date ? (date.innerText || date.textContent || '').replace(/\s+/g, ' ').trim() : '',
    };
  }).filter(comment => comment.content);
}
"""


async def _check_login_or_raise() -> None:
    cookies = await pool.get_cookies("xhs")
    if not pool.is_logged_in("xhs", cookies):
        raise ScraperError(
            "xhs requires login. Call POST /api/browser/login/xhs first to authenticate.",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="browser-login",
        )


async def _raise_if_login_wall(capability: str) -> None:
    logged_out = await pool.evaluate("xhs", _AUTH_STATE_JS, timeout_ms=5000)
    if logged_out:
        raise ScraperError(
            "xhs browser login is missing or expired",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability=capability,
        )


class XhsScraper(BaseScraper):
    platform = "xhs"
    degrades_to_mock = False

    @staticmethod
    def _search_result(raw: dict) -> dict:
        note_id = str(raw.get("note_id") or "")
        token = str(raw.get("xsec_token") or "")
        source = str(raw.get("xsec_source") or "")
        user = raw.get("user") or {}
        author = raw.get("author") or user.get("nickname") or ""
        user_id = str(raw.get("user_id") or user.get("user_id") or user.get("userId") or "")
        detail_url = _detail_url(note_id, token, source) if token else (raw.get("detail_url") or _detail_url(note_id, token, source))
        return {
            "content_id": note_id,
            "title": re.sub(r"<[^>]+>", "", str(raw.get("title") or "")),
            "author": author,
            "url": detail_url,
            "stats": {
                "view": None,
                "like": _optional_int(raw.get("liked_count")),
                "collect": _optional_int(raw.get("collected_count")),
                "reply": _optional_int(raw.get("comment_count")),
                "share": _optional_int(raw.get("share_count")),
            },
            "cover": raw.get("cover") or "",
            "xsec_token": token,
            "xsec_source": source,
            "user_id": user_id,
            "note_id": note_id,
        }

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        await _check_login_or_raise()
        query = urlencode({"keyword": str(keyword or ""), "source": "web_explore_feed", "type": "51"})
        await _navigate_xhs_page(
            f"https://www.xiaohongshu.com/search_result?{query}",
            "/search_result",
            timeout_ms=20000,
        )
        try:
            await pool.wait_for_state("xhs", _SEARCH_WAIT_JS, timeout_ms=12000)
        except ScraperError:
            logger.warning("xhs search state not ready: %s", keyword)
        feeds = await pool.evaluate("xhs", _EXTRACT_SEARCH_JS, timeout_ms=10000)
        if not isinstance(feeds, list):
            await _raise_if_login_wall("search")
            return []
        return [self._search_result(item) for item in feeds[: max(0, limit)] if item.get("note_id")]

    async def get_user_contents(
        self,
        identifier: str,
        *,
        account_name: str = "",
        limit: int = 20,
    ) -> list[dict]:
        await pool.restore_xhs_profile_page()
        await _check_login_or_raise()
        user_id, profile_url = _profile_from_identifier(identifier)
        if not user_id:
            if not account_name:
                raise ScraperError(
                    "xhs account sync requires a profile URL/userId or account_name",
                    code=ErrorCode.INVALID_CONTENT_ID,
                    status_code=400,
                    retryable=False,
                    capability="account-contents",
                )
            candidates = await self.search(account_name, limit=max(limit * 3, limit))
            exact: list[dict] = []
            for item in candidates:
                if not _names_equal(item.get("author", ""), account_name):
                    continue
                result = dict(item)
                result["discovery_strength"] = "weak_exact_name"
                result["profile_url"] = ""
                exact.append(result)
                if len(exact) >= limit:
                    break
            return exact

        stable_profile_url = _PROFILE_URL.format(user_id=quote(user_id, safe=""))
        await _navigate_xhs_page(
            profile_url,
            f"/user/profile/{quote(user_id, safe='')}",
            timeout_ms=20000,
            allow_js_fallback=False,
        )
        try:
            await pool.wait_for_state("xhs", _PROFILE_WAIT_JS, timeout_ms=12000)
        except ScraperError:
            logger.warning("xhs profile state not ready: %s", user_id)
        try:
            profile_state = await pool.evaluate("xhs", _PAGE_STATE_JS, "", timeout_ms=5000)
        except ScraperError:
            profile_state = {}
        await _raise_for_profile_page_state(
            user_id,
            profile_state if isinstance(profile_state, dict) else {},
        )
        raw = await pool.evaluate("xhs", _EXTRACT_PROFILE_JS, user_id, timeout_ms=15000)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            await _raise_if_login_wall("account-contents")
            return []
        profile_author = raw.get("profile_author") or account_name
        profile_user_id = str(raw.get("profile_user_id") or user_id)
        results: list[dict] = []
        seen: set[str] = set()
        for item in raw["items"]:
            note_id = str(item.get("note_id") or "")
            if not note_id or note_id in seen:
                continue
            seen.add(note_id)
            if not item.get("author"):
                item["author"] = profile_author
            if not item.get("user_id"):
                item["user_id"] = profile_user_id
            result = self._search_result(item)
            result["url"] = _detail_url(note_id)
            result.pop("xsec_token", None)
            result.pop("xsec_source", None)
            result["discovery_strength"] = "strong"
            result["profile_url"] = stable_profile_url
            results.append(result)
            if len(results) >= limit:
                break
        return results

    async def get_info(self, content_id: str, *, account_import: bool = False) -> dict:
        note_id, token, source = _parse_note_identifier(content_id)
        if not note_id:
            raise ScraperError(
                f"invalid xhs note id: {content_id!r}",
                code=ErrorCode.INVALID_CONTENT_ID,
                status_code=400,
                retryable=False,
                capability="info",
            )
        url = "" if account_import else _detail_url(note_id, token, source)
        value = None if account_import else (
            await _extract_existing_chrome_detail(note_id) if _USE_EXISTING_CHROME_TAB else None
        )
        pool_error: ScraperError | None = None
        page_state: dict = {}
        if not value or not (value.get("title") or value.get("cover") or value.get("images")):
            try:
                await _check_login_or_raise()
                page_state = await _open_detail_page(note_id, url, account_import=account_import)
                await _raise_for_detail_page_state(note_id, page_state, "info")
                candidate = await pool.evaluate("xhs", _EXTRACT_DETAIL_JS, note_id, timeout_ms=10000)
                if isinstance(candidate, dict):
                    value = candidate
            except ScraperError as exc:
                pool_error = exc
                if account_import:
                    await pool.restore_xhs_profile_page()
        if not value or not (value.get("title") or value.get("cover") or value.get("images")):
            if account_import:
                await pool.restore_xhs_profile_page()
            if pool_error and (
                pool_error.code in {ErrorCode.CREDENTIAL_EXPIRED, ErrorCode.RATE_LIMITED}
                or pool_error.capability == "account-card-navigation"
            ):
                raise pool_error
            await _raise_for_detail_page_state(note_id, page_state, "info")
            raise ScraperError(
                f"xhs info not found: {note_id}",
                code=ErrorCode.CONTENT_NOT_FOUND,
                status_code=404,
                retryable=False,
                capability="info",
            )
        user = value.get("user") or {}
        declared_media_type = str(value.get("media_type") or "").lower()
        media_type = "video" if declared_media_type == "video" or (isinstance(value.get("video"), dict) and value.get("video")) else "image"
        images = [str(item) for item in (value.get("images") or []) if item]
        cover = value.get("cover") or (images[0] if images else "")
        if media_type == "video":
            images = []
        elif cover and cover not in images:
            images.insert(0, cover)
        metrics = {
            "view_count": _optional_int(value.get("view_count")),
            "play_count": _optional_int(value.get("play_count")),
            "liked_count": _optional_int(value.get("liked_count")),
            "collected_count": _optional_int(value.get("collected_count")),
            "comment_count": _optional_int(value.get("comment_count")),
            "share_count": _optional_int(value.get("share_count")),
        }
        return {
            "content_id": value.get("note_id") or note_id,
            "title": (value.get("title") or "")[:240],
            "description": (value.get("description") or "")[:2000],
            "user": {"nickname": user.get("nickname") or user.get("nickName") or ""},
            "cover": cover,
            "images": images,
            "media_type": media_type,
            "url": value.get("url") or url,
            "publish_time": _optional_int(value.get("publish_time")),
            "interact_info": metrics,
            "metric_presence": {key: metric is not None for key, metric in metrics.items()},
        }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        note_id, token, source = _parse_note_identifier(content_id)
        if not note_id:
            return []
        reuse_current_page = bool(kwargs.pop("reuse_current_page", False))
        account_import = bool(kwargs.pop("account_import", False))
        comments = await _extract_existing_chrome_comments(note_id) if _USE_EXISTING_CHROME_TAB else None
        if comments is None:
            try:
                await _check_login_or_raise()
                if reuse_current_page:
                    page_state = await _detail_page_state(note_id)
                else:
                    page_state = await _open_detail_page(
                        note_id,
                        "" if account_import else _detail_url(note_id, token, source),
                        account_import=account_import,
                    )
                await _raise_for_detail_page_state(note_id, page_state, "comments")
                if reuse_current_page and not (page_state.get("target_match") and page_state.get("detail_ready")):
                    if account_import:
                        await pool.restore_xhs_profile_page()
                    return []
                try:
                    await pool.evaluate(
                        "xhs",
                        "() => { const area = document.querySelector('.comments-container'); if (area) area.scrollIntoView({block: 'start'}); return true; }",
                        timeout_ms=5000,
                    )
                except ScraperError:
                    pass
                try:
                    await pool.wait_for_state("xhs", _COMMENT_WAIT_JS, timeout_ms=8000)
                except ScraperError:
                    logger.warning("xhs comments not loaded: %s", note_id)
                candidate = await pool.evaluate("xhs", _EXTRACT_COMMENTS_JS, timeout_ms=10000)
                if isinstance(candidate, list):
                    comments = candidate
            except ScraperError as exc:
                if exc.code in {ErrorCode.CREDENTIAL_EXPIRED, ErrorCode.RATE_LIMITED}:
                    if account_import:
                        await pool.restore_xhs_profile_page()
                    raise
                comments = None
        if not isinstance(comments, list):
            if account_import:
                await pool.restore_xhs_profile_page()
            return []
        if account_import:
            await pool.restore_xhs_profile_page()
        return [
            {
                "user_info": {
                    "nickname": item.get("nickname") or "",
                    "user_id": item.get("userId") or None,
                    "profile_url": item.get("profileUrl") or None,
                },
                "content": item.get("content") or "",
                "like_count": _optional_int(item.get("likeCount")),
                "create_time": int(item.get("createTime") or 0) * 1000 or None,
                "create_time_text": item.get("createTimeText") or "",
                "sub_comments": [],
            }
            for item in comments[:max_comments]
            if str(item.get("content") or "").strip()
        ]
