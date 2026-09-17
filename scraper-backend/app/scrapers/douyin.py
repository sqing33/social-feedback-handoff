"""抖音抓取器（持久浏览器页面 + React props/DOM 提取）。"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .base import BaseScraper
from ..browser.pool import pool
from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.douyin")

_SEARCH_URL = "https://www.douyin.com/search/{keyword}?type=general"
_DETAIL_URL = "https://www.douyin.com/video/{aweme_id}"
_PROFILE_URL = "https://www.douyin.com/user/{sec_uid}"


def _normalize_account_name(value: str) -> str:
    """Only remove presentation differences; this is deliberately not fuzzy matching."""
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
        if not parsed.hostname or not parsed.hostname.lower().endswith("douyin.com"):
            return "", ""
        match = re.search(r"/user/([^/?#]+)", parsed.path)
        if not match:
            return "", ""
        sec_uid = unquote(match.group(1))
        return sec_uid, _PROFILE_URL.format(sec_uid=quote(sec_uid, safe=""))
    return raw, _PROFILE_URL.format(sec_uid=quote(raw, safe=""))


# Search cards expose awemeInfo through their React props.
_EXTRACT_SEARCH_JS = r"""
() => {
  const parseMetric = (value) => {
    if (value === undefined || value === null || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? Math.trunc(n) : null;
  };
  const firstUrl = (value) => {
    if (!value) return '';
    if (typeof value === 'string') return value;
    const urls = value.url_list || value.urlList;
    return Array.isArray(urls) ? (urls[0] || '') : '';
  };
  const cards = document.querySelectorAll('.search-result-card');
  const out = [];
  for (const card of Array.from(cards).slice(0, 30)) {
    try {
      const key = Object.keys(card).find(k => k.startsWith('__reactProps'));
      if (!key) continue;
      const props = card[key];
      const data = props && props.children && props.children.props
        && props.children.props.children && props.children.props.children.props
        && props.children.props.children.props.data;
      const aw = data && data.awemeInfo;
      if (!aw) continue;
      const author = aw.authorInfo || aw.author || {};
      const video = aw.video || {};
      const stats = aw.statistics || aw.stats || {};
      const id = String(aw.awemeId || aw.aweme_id || '');
      if (!id) continue;
      out.push({
        aweme_id: id,
        desc: String(aw.desc || '').replace(/<[^>]+>/g, ''),
        author: author.nickname || author.nickName || '',
        author_id: author.uid || author.secUid || author.sec_uid || '',
        author_avatar: firstUrl(author.avatarThumb || author.avatar_thumb) || author.avatarUri || '',
        duration_ms: parseMetric(video.duration),
        cover: firstUrl(video.cover || video.originCover || video.dynamicCover),
        share_url: aw.share_url || aw.shareUrl || ('https://www.douyin.com/video/' + id),
        like: parseMetric(stats.digg_count ?? stats.diggCount ?? stats.like_count ?? stats.likeCount),
        comment: parseMetric(stats.comment_count ?? stats.commentCount),
        collect: parseMetric(stats.collect_count ?? stats.collectCount),
        share: parseMetric(stats.share_count ?? stats.shareCount),
        play: parseMetric(stats.play_count ?? stats.playCount),
        create_time: parseMetric(aw.create_time ?? aw.createTime),
      });
    } catch (_) {
      // One malformed card must not hide the remaining real cards.
    }
  }
  return out;
}
"""


# A profile page can expose complete objects in React props and, at minimum, real
# /video/<id> anchors. React objects are filtered by the current profile secUid.
_EXTRACT_PROFILE_JS = r"""
(targetSecUid) => {
  const target = String(targetSecUid || '');
  const out = new Map();
  const firstUrl = (value) => {
    if (!value) return '';
    if (typeof value === 'string') return value;
    const urls = value.url_list || value.urlList;
    return Array.isArray(urls) ? (urls[0] || '') : '';
  };
  const metric = (obj, keys) => {
    if (!obj || typeof obj !== 'object') return null;
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) {
        const n = Number(obj[key]);
        return Number.isFinite(n) ? Math.trunc(n) : null;
      }
    }
    return null;
  };
  const profileAuthor = (() => {
    const selectors = [
      '[data-e2e="user-title"]', '[data-e2e="user-name"]',
      'h1', '[class*="user-info"] [class*="name"]'
    ];
    for (const selector of selectors) {
      const el = document.querySelector(selector);
      const text = el && (el.innerText || el.textContent || '').trim();
      if (text) return text;
    }
    return '';
  })();
  const addAweme = (aw) => {
    if (!aw || typeof aw !== 'object') return;
    const id = String(aw.awemeId || aw.aweme_id || '');
    if (!id) return;
    const author = aw.authorInfo || aw.author || {};
    const authorSecUid = String(author.secUid || author.sec_uid || '');
    if (target && authorSecUid && authorSecUid !== target) return;
    const video = aw.video || {};
    const stats = aw.statistics || aw.stats || {};
    out.set(id, {
      aweme_id: id,
      desc: String(aw.desc || aw.title || '').replace(/<[^>]+>/g, ''),
      author: author.nickname || author.nickName || profileAuthor,
      author_avatar: firstUrl(author.avatarThumb || author.avatar_thumb) || author.avatarUri || '',
      duration_ms: metric(video, ['duration']),
      cover: firstUrl(video.cover || video.originCover || video.dynamicCover),
      like: metric(stats, ['digg_count', 'diggCount', 'like_count', 'likeCount']),
      comment: metric(stats, ['comment_count', 'commentCount']),
      collect: metric(stats, ['collect_count', 'collectCount']),
      share: metric(stats, ['share_count', 'shareCount']),
      play: metric(stats, ['play_count', 'playCount']),
      url: 'https://www.douyin.com/video/' + id,
    });
  };
  const scan = (root) => {
    if (!root || typeof root !== 'object') return;
    const stack = [{value: root, depth: 0}];
    const seen = new WeakSet();
    let visited = 0;
    while (stack.length && visited < 1200) {
      const {value, depth} = stack.pop();
      if (!value || typeof value !== 'object' || seen.has(value)) continue;
      seen.add(value);
      visited += 1;
      if (value.awemeInfo) addAweme(value.awemeInfo);
      if ((value.awemeId || value.aweme_id) && (value.video || value.statistics || value.stats)) addAweme(value);
      if (depth >= 8) continue;
      for (const child of Object.values(value)) {
        if (child && typeof child === 'object') stack.push({value: child, depth: depth + 1});
      }
    }
  };

  const scanElementProps = (el) => {
    if (!el) return;
    for (const key of Object.keys(el)) {
      if (key.startsWith('__reactProps') || key.startsWith('__reactFiber')) scan(el[key]);
    }
  };
  const globalRoots = [
    window.__INITIAL_STATE__, window.__INITIAL_STATE, window.__NEXT_DATA__,
    window._ROUTER_DATA, window.__ROUTER_DATA__, window.__REACT_QUERY_STATE__
  ];
  for (const root of globalRoots) scan(root);
  scanElementProps(document.getElementById('root'));
  scanElementProps(document.body);

  const reactHosts = Array.from(document.querySelectorAll(
    'main a[href*="/video/"], main li, [data-e2e*="post"], [data-e2e*="user-post"]'
  )).slice(0, 240);
  for (const el of reactHosts) {
    for (const key of Object.keys(el)) {
      if (key.startsWith('__reactProps') || key.startsWith('__reactFiber')) scan(el[key]);
    }
  }

  // DOM fallback still uses only links physically present in the current profile's main area.
  const anchors = document.querySelectorAll('main a[href*="/video/"], [data-e2e*="user-post"] a[href*="/video/"]');
  for (const anchor of Array.from(anchors).slice(0, 100)) {
    const href = anchor.href || anchor.getAttribute('href') || '';
    const match = href.match(/\/video\/(\d+)/);
    if (!match) continue;
    const id = match[1];
    if (out.has(id)) continue;
    const card = anchor.closest('li, [class*="card"], [class*="item"]') || anchor;
    const image = card.querySelector('img') || anchor.querySelector('img');
    const title = (image && (image.alt || image.getAttribute('aria-label'))) || anchor.getAttribute('title') || '';
    out.set(id, {
      aweme_id: id,
      desc: String(title || '').trim(),
      author: profileAuthor,
      author_avatar: '',
      duration_ms: null,
      cover: image ? (image.currentSrc || image.src || '') : '',
      like: null, comment: null, collect: null, share: null, play: null,
      url: 'https://www.douyin.com/video/' + id,
    });
  }
  return {profile_author: profileAuthor, items: Array.from(out.values())};
}
"""


_EXTRACT_DETAIL_JS = r"""
() => {
  const pathMatch = location.pathname.match(/\/video\/(\d+)/);
  const awemeId = pathMatch ? pathMatch[1] : '';
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    const value = Number.parseFloat(match[1]);
    return Number.isFinite(value) ? Math.round(value * factor) : null;
  };
  const textOf = (selector) => {
    const el = document.querySelector(selector);
    return el ? (el.innerText || el.textContent || '').trim() : '';
  };
  const metricOf = (selector) => {
    const el = document.querySelector(selector);
    return el ? parseCount(el.innerText || el.textContent || '') : null;
  };
  const meta = (selector) => document.querySelector(selector)?.content || '';
  const title = textOf('[data-e2e="video-desc"]')
    || textOf('[data-e2e="detail-video-title"]')
    || textOf('h1')
    || meta('meta[property="og:title"]')
    || document.title.replace(/\s*-\s*抖音\s*$/, '');
  const authorNode = Array.from(document.querySelectorAll('[data-click-from="title"]'))
    .find(el => !el.closest('[data-e2e="comment-item"]'));
  const author = (authorNode ? (authorNode.innerText || authorNode.textContent || '').trim() : '')
    || textOf('[data-e2e="video-author-name"]');
  const cover = meta('meta[property="lark:url:video_cover_image_url"]')
    || meta('meta[property="og:image"]')
    || document.querySelector('video')?.poster
    || Array.from(document.images).map(img => img.currentSrc || img.src || '').find(src => src.includes('PackSourceEnum_AWEME_DETAIL'))
    || '';
  const bodyText = document.body ? document.body.innerText || '' : '';
  const publishedMatch = bodyText.match(/发布时间[：:]\s*(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/);
  let createTime = null;
  if (publishedMatch) {
    createTime = Math.floor(new Date(`${publishedMatch[1]}-${publishedMatch[2]}-${publishedMatch[3]}T${publishedMatch[4]}:${publishedMatch[5]}:00+08:00`).getTime() / 1000);
  }
  return {
    aweme_id: awemeId,
    desc: title.trim(),
    description: (meta('meta[property="og:description"]') || title).trim(),
    author,
    cover,
    like: metricOf('[data-e2e="video-player-digg"]'),
    collect: metricOf('[data-e2e="video-player-collect"]'),
    comment: metricOf('[data-e2e="feed-comment-icon"]'),
    share: metricOf('[data-e2e="video-player-share"]'),
    play: metricOf('[data-e2e="video-player-play-count"]'),
    create_time: createTime,
    url: location.href,
    found: !!(awemeId && title.trim()),
  };
}
"""


_EXTRACT_COMMENTS_JS = r"""
() => {
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    const value = Number.parseFloat(match[1]);
    return Number.isFinite(value) ? Math.round(value * factor) : null;
  };
  const textOf = (root, selector) => {
    const el = root.querySelector(selector);
    return el ? (el.innerText || el.textContent || '').trim() : '';
  };
  return Array.from(document.querySelectorAll('[data-e2e="comment-item"]')).slice(0, 50).map(el => {
    const authorNode = el.querySelector('.comment-item-info-wrap [data-click-from="title"]')
      || el.querySelector('.comment-item-info-wrap a[href*="/user/"]');
    const contentNode = el.querySelector('.comment-item-info-wrap')?.parentElement?.querySelector('.Sh1Da424')
      || el.querySelector('.FduGc_lz')
      || el.querySelector('[class*="comment-item-content"]');
    const timeNode = el.querySelector('.VAQA49VP')
      || Array.from(el.querySelectorAll('span, div')).find(node => /(?:刚刚|前|昨天|\d{1,2}[-月]\d{1,2})/.test((node.innerText || '').trim()) && /(?:·|刚刚|前|昨天|\d)/.test((node.innerText || '').trim()));
    const likeNode = el.querySelector('.comment-item-stats-container .jx2w9iES span:last-child')
      || el.querySelector('.comment-item-stats-container .jx2w9iES')
      || el.querySelector('.comment-item-stats-container p span:last-child');
    return {
      nickname: authorNode ? (authorNode.innerText || authorNode.textContent || '').trim() : '',
      content: contentNode ? (contentNode.innerText || contentNode.textContent || '').trim() : '',
      like_count: likeNode ? parseCount(likeNode.innerText || likeNode.textContent || '') : null,
      create_time_text: timeNode ? (timeNode.innerText || timeNode.textContent || '').trim() : '',
    };
  }).filter(item => item.nickname || item.content);
}
"""


async def _check_login_or_raise() -> None:
    cookies = await pool.get_cookies("douyin")
    if not pool.is_logged_in("douyin", cookies):
        raise ScraperError(
            "douyin requires login. Call POST /api/browser/login/douyin first to authenticate.",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="browser-login",
        )


class DouyinScraper(BaseScraper):
    platform = "douyin"
    degrades_to_mock = False

    @staticmethod
    def _search_result(raw: dict) -> dict:
        aweme_id = str(raw.get("aweme_id") or "")
        return {
            "content_id": aweme_id,
            "title": (raw.get("desc") or "")[:120],
            "author": raw.get("author") or "",
            "url": raw.get("share_url") or raw.get("url") or (_DETAIL_URL.format(aweme_id=aweme_id) if aweme_id else ""),
            "stats": {
                "view": _optional_int(raw.get("play")),
                "like": _optional_int(raw.get("like")),
                "collect": _optional_int(raw.get("collect")),
                "reply": _optional_int(raw.get("comment")),
                "share": _optional_int(raw.get("share")),
            },
            "cover": raw.get("cover") or "",
            "author_avatar": raw.get("author_avatar") or "",
            "duration_ms": _optional_int(raw.get("duration_ms")),
            "aweme_id": aweme_id,
        }

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        await _check_login_or_raise()
        url = _SEARCH_URL.format(keyword=quote(str(keyword or ""), safe=""))
        await pool.navigate("douyin", url, wait_until="domcontentloaded", timeout_ms=20000)
        await asyncio_sleep(4)
        for _ in range(20):
            count = await pool.evaluate(
                "douyin",
                "() => document.querySelectorAll('.search-result-card').length",
                timeout_ms=5000,
            )
            if count and count >= 3:
                break
            await asyncio_sleep(0.5)
        items = await pool.evaluate("douyin", _EXTRACT_SEARCH_JS, timeout_ms=15000)
        if not isinstance(items, list):
            return []
        return [self._search_result(item) for item in items[: max(0, limit)] if item.get("aweme_id")]

    async def get_user_contents(
        self,
        identifier: str,
        *,
        account_name: str = "",
        limit: int = 20,
    ) -> list[dict]:
        await _check_login_or_raise()
        sec_uid, profile_url = _profile_from_identifier(identifier)
        if not sec_uid:
            if not account_name:
                raise ScraperError(
                    "douyin account sync requires a profile URL/secUid or account_name",
                    code=ErrorCode.INVALID_CONTENT_ID,
                    status_code=400,
                    retryable=False,
                    capability="account-contents",
                )
            matches = await self.search(account_name, limit=max(limit * 3, limit))
            exact: list[dict] = []
            for item in matches:
                if not _names_equal(item.get("author", ""), account_name):
                    continue
                result = dict(item)
                result["discovery_strength"] = "weak_exact_name"
                result["profile_url"] = ""
                exact.append(result)
                if len(exact) >= limit:
                    break
            return exact

        await pool.navigate("douyin", profile_url, wait_until="domcontentloaded", timeout_ms=20000)
        try:
            await pool.wait_for_state(
                "douyin",
                "() => document.querySelectorAll('main a[href*=\"/video/\"], [data-e2e*=\"user-post\"] a[href*=\"/video/\"]').length > 0",
                timeout_ms=12000,
            )
        except ScraperError:
            logger.warning("douyin profile items not ready: %s", sec_uid)
        raw = await pool.evaluate("douyin", _EXTRACT_PROFILE_JS, sec_uid, timeout_ms=15000)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            return []
        profile_author = raw.get("profile_author") or account_name
        results: list[dict] = []
        seen: set[str] = set()
        for item in raw["items"]:
            aweme_id = str(item.get("aweme_id") or "")
            if not aweme_id or aweme_id in seen:
                continue
            seen.add(aweme_id)
            if not item.get("author"):
                item["author"] = profile_author
            result = self._search_result(item)
            result["discovery_strength"] = "strong"
            result["profile_url"] = profile_url
            results.append(result)
            if len(results) >= limit:
                break
        return results

    async def get_info(self, content_id: str) -> dict:
        match = re.search(r"(?:/video/)?(\d+)", str(content_id or ""))
        aweme_id = match.group(1) if match else ""
        if not aweme_id:
            raise ScraperError(
                f"invalid douyin aweme id: {content_id!r}",
                code=ErrorCode.INVALID_CONTENT_ID,
                status_code=400,
                retryable=False,
                capability="info",
            )
        url = _DETAIL_URL.format(aweme_id=quote(aweme_id, safe=""))

        async def load_detail() -> dict:
            await pool.navigate(
                "douyin",
                url,
                wait_until="domcontentloaded",
                timeout_ms=20000,
            )
            try:
                await pool.wait_for_state(
                    "douyin",
                    "() => !!document.querySelector('h1, [data-e2e=\"video-desc\"], [data-e2e=\"detail-video-title\"]') && !!document.querySelector('[data-e2e=\"video-player-digg\"]')",
                    timeout_ms=10000,
                )
            except ScraperError:
                logger.warning("douyin detail fields not ready before extraction: %s", aweme_id)
            await asyncio_sleep(0.3)
            result = await pool.evaluate("douyin", _EXTRACT_DETAIL_JS, timeout_ms=15000)
            return result if isinstance(result, dict) else {}

        value: dict[str, Any] = {}
        for attempt in range(2):
            value = await load_detail()
            if value.get("found") and str(value.get("desc") or "").strip():
                break
            if attempt == 0:
                logger.warning("douyin detail missing title; retrying once: %s", aweme_id)
                await asyncio_sleep(1.0)

        if not value or not value.get("found"):
            raise ScraperError(
                f"douyin info not found: {content_id}",
                code=ErrorCode.CONTENT_NOT_FOUND,
                status_code=404,
                retryable=False,
                capability="info",
            )
        cover = value.get("cover") or ""
        metrics = {
            "view_count": _optional_int(value.get("play")),
            "play_count": _optional_int(value.get("play")),
            "liked_count": _optional_int(value.get("like")),
            "collected_count": _optional_int(value.get("collect")),
            "comment_count": _optional_int(value.get("comment")),
            "share_count": _optional_int(value.get("share")),
        }
        return {
            "content_id": value.get("aweme_id") or aweme_id,
            "title": (value.get("desc") or "")[:240],
            "description": (value.get("description") or value.get("desc") or "")[:2000],
            "user": {"nickname": value.get("author") or ""},
            "cover": cover,
            "images": [cover] if cover else [],
            "media_type": "video",
            "url": value.get("url") or url,
            "publish_time": _optional_int(value.get("create_time")),
            "interact_info": metrics,
            "metric_presence": {key: metric is not None for key, metric in metrics.items()},
        }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        await self.get_info(content_id)
        target_count = max(1, min(int(max_comments), 5))
        try:
            await pool.wait_for_state(
                "douyin",
                f"() => document.querySelectorAll('[data-e2e=\"comment-item\"]').length >= {target_count}",
                timeout_ms=8000,
            )
        except ScraperError:
            logger.warning("douyin rendered fewer than %s comments before extraction", target_count)
        comments = await pool.evaluate("douyin", _EXTRACT_COMMENTS_JS, timeout_ms=10000)
        if not isinstance(comments, list):
            return []
        return [
            {
                "user_info": {"nickname": item.get("nickname") or ""},
                "content": item.get("content") or "",
                "like_count": _optional_int(item.get("like_count")),
                "create_time": None,
                "create_time_text": item.get("create_time_text") or None,
                "sub_comments": [],
            }
            for item in comments[: max(0, int(max_comments))]
            if item.get("nickname") or item.get("content")
        ]


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
