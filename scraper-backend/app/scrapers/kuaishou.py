"""快手抓取器（持久浏览器页面 + DOM/Apollo state 提取）。"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from .base import BaseScraper
from ..browser.pool import pool
from ..errors import ErrorCode, ScraperError

logger = logging.getLogger("scraper.kuaishou")

_SEARCH_URL = "https://www.kuaishou.com/search/video?searchKey={keyword}"
_DETAIL_URL = "https://www.kuaishou.com/short-video/{photo_id}"
_PROFILE_URL = "https://www.kuaishou.com/profile/{user_id}"


def _normalize_account_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    value = re.sub(r"^@", "", value).strip()
    return re.sub(r"\s+", " ", value).casefold()


def _names_equal(left: str, right: str) -> bool:
    expected = _normalize_account_name(right)
    return bool(expected) and _normalize_account_name(left) == expected


def _normalize_photo_id(value: Any) -> str:
    """clientCacheKey is sometimes ``<photo_id>_ccc``; _ccc is not part of the ID."""
    raw = unquote(str(value or "")).strip().split("?", 1)[0].rstrip("/")
    match = re.search(r"/short-video/([^/?#]+)", raw)
    if match:
        raw = match.group(1)
    return re.sub(r"_ccc$", "", raw)


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
        if not parsed.hostname or not parsed.hostname.lower().endswith("kuaishou.com"):
            return "", ""
        match = re.search(r"/(?:profile|user)/([^/?#]+)", parsed.path)
        if not match:
            return "", ""
        user_id = unquote(match.group(1))
        return user_id, _PROFILE_URL.format(user_id=quote(user_id, safe=""))
    return raw, _PROFILE_URL.format(user_id=quote(raw, safe=""))


_WAIT_JS = r"""
() => document.querySelectorAll('a[href*="/short-video/"], .photo-card, [class*="video-card"]').length > 0
"""


# The current /search/video page has changed card class names several times. The
# stable primitive is the real /short-video/<id> link, so extraction starts there.
_EXTRACT_SEARCH_JS = r"""
() => {
  const results = new Map();
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    const n = Number.parseFloat(match[1]);
    return Number.isFinite(n) ? Math.round(n * factor) : null;
  };
  const normalizeId = (value) => decodeURIComponent(String(value || '')).replace(/_ccc$/, '');
  const idFromCover = (src) => {
    if (!src) return '';
    try {
      const url = new URL(src, location.href);
      const key = url.searchParams.get('clientCacheKey');
      if (key) return normalizeId(key.split('.')[0]);
    } catch (_) {}
    const last = String(src).split('/').pop().split('?')[0].split('.')[0];
    return normalizeId(last);
  };
  const backgroundUrl = (element) => {
    if (!element) return '';
    const value = element.style.backgroundImage || getComputedStyle(element).backgroundImage || '';
    const match = value.match(/url\(["']?(.*?)["']?\)/);
    return match ? match[1] : '';
  };
  const readCard = (card, anchor) => {
    const href = anchor ? (anchor.href || anchor.getAttribute('href') || '') : '';
    const hrefMatch = href.match(/\/short-video\/([^/?#]+)/);
    const image = card.querySelector('img.cover-img, .cover img, img');
    const cover = image ? (image.currentSrc || image.src || '') : backgroundUrl(card.querySelector('.cover, [class*="cover"]'));
    let photoId = hrefMatch ? normalizeId(hrefMatch[1]) : idFromCover(cover);
    if (!photoId) return;
    const titleEl = card.querySelector('.caption, .video-info-title, [class*="caption"], [class*="title"]');
    const authorEl = card.querySelector('.info .user .name, .info .user .header, .profile-user-name-title, [class*="author"] [class*="name"], [class*="user"] [class*="name"]');
    const likeEl = card.querySelector('[data-like-count], .like-count, [class*="like"] [class*="count"], [class*="like-count"]');
    const likeRaw = (likeEl && (likeEl.getAttribute('data-like-count') || likeEl.innerText || likeEl.textContent))
      || card.getAttribute('data-like-count') || '';
    const fullTitle = (titleEl ? titleEl.innerText || titleEl.textContent || '' : '').replace(/\s+/g, ' ').trim();
    const current = results.get(photoId) || {};
    results.set(photoId, {
      photo_id: photoId,
      caption: fullTitle.split('#')[0].trim() || current.caption || '',
      tags: (fullTitle.match(/#[\u4e00-\u9fa5\w]+/g) || []).map(t => t.slice(1)),
      author: (authorEl ? authorEl.innerText || authorEl.textContent || '' : '').trim() || current.author || '',
      cover: cover || current.cover || '',
      like_count: parseCount(likeRaw),
      url: href ? new URL(href, location.href).href : ('https://www.kuaishou.com/short-video/' + photoId),
    });
  };

  for (const anchor of Array.from(document.querySelectorAll('a[href*="/short-video/"]')).slice(0, 100)) {
    const card = anchor.closest('.photo-card, .video-card, [class*="photo-card"], [class*="video-card"], [class*="card-container"], li') || anchor;
    readCard(card, anchor);
  }
  if (!results.size) {
    for (const card of Array.from(document.querySelectorAll('.photo-card, [class*="photo-card"], [class*="video-card"]')).slice(0, 50)) {
      readCard(card, card.querySelector('a[href*="/short-video/"]'));
    }
  }
  return Array.from(results.values());
}
"""


_EXTRACT_PROFILE_JS = r"""
(targetUserId) => {
  const target = String(targetUserId || '');
  const profileAuthor = (() => {
    for (const selector of ['.profile-user-name-title', '.profile-user-name', '[class*="profile"] [class*="user-name"]', 'h1']) {
      const element = document.querySelector(selector);
      const text = element && (element.innerText || element.textContent || '').trim();
      if (text) return text;
    }
    return '';
  })();
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
  const normalizeId = (value) => decodeURIComponent(String(value || '')).replace(/_ccc$/, '');
  const pick = (obj, keys) => {
    if (!obj || typeof obj !== 'object') return undefined;
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];
    }
    return undefined;
  };
  const pickUrl = (value, depth = 0) => {
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
      for (const key of ['url', 'urlDefault', 'coverUrl', 'photoUrl', 'cdn']) {
        if (typeof value[key] === 'string' && value[key]) return value[key];
      }
      for (const child of Object.values(value)) {
        const found = pickUrl(child, depth + 1);
        if (found) return found;
      }
    }
    return '';
  };
  const idFromCover = (src) => {
    if (!src) return '';
    try {
      const url = new URL(src, location.href);
      const key = url.searchParams.get('clientCacheKey');
      if (key) return normalizeId(key.split('.')[0]);
    } catch (_) {}
    return '';
  };
  const results = new Map();
  const merge = (item) => {
    const photoId = normalizeId(item.photo_id);
    if (!photoId) return;
    const current = results.get(photoId) || {};
    results.set(photoId, {
      photo_id: photoId,
      caption: item.caption || current.caption || '',
      tags: item.tags || current.tags || [],
      author: item.author || current.author || profileAuthor,
      cover: item.cover || current.cover || '',
      like_count: item.like_count ?? current.like_count ?? null,
      view_count: item.view_count ?? current.view_count ?? null,
      collect_count: item.collect_count ?? current.collect_count ?? null,
      comment_count: item.comment_count ?? current.comment_count ?? null,
      url: 'https://www.kuaishou.com/short-video/' + photoId,
    });
  };

  // Profile Apollo data can hold real photo objects without rendering anchors.
  const state = window.__APOLLO_STATE__ || {};
  const seen = new WeakSet();
  const walk = (value, depth = 0) => {
    if (!value || typeof value !== 'object' || depth > 9 || seen.has(value)) return;
    seen.add(value);
    if (!Array.isArray(value)) {
      const author = value.author || value.user || value.owner || value.userInfo || {};
      const authorId = String(pick(author, ['id', 'userId', 'user_id', 'kwaiId']) || pick(value, ['userId', 'authorId']) || '');
      const cover = pickUrl(pick(value, ['coverUrl', 'coverUrls', 'cover'])) || pickUrl(value.coverUrl);
      const photoId = normalizeId(pick(value, ['photoId', 'photo_id', 'clientCacheKey']) || idFromCover(cover));
      const looksLikePhoto = !!(photoId && (pick(value, ['caption', 'title', 'description']) || cover || pick(value, ['realLikeCount', 'likeCount'])));
      if (looksLikePhoto && (!authorId || !target || authorId === target)) {
        const caption = String(pick(value, ['caption', 'title', 'description']) || '');
        merge({
          photo_id: photoId,
          caption,
          tags: (caption.match(/#[\u4e00-\u9fa5\w]+/g) || []).map(tag => tag.slice(1)),
          author: String(pick(author, ['name', 'userName', 'nickname']) || profileAuthor),
          cover,
          like_count: parseCount(pick(value, ['realLikeCount', 'likeCount', 'likedCount'])),
          view_count: parseCount(pick(value, ['viewCount', 'realViewCount', 'playCount'])),
          collect_count: parseCount(pick(value, ['collectCount', 'collectedCount', 'favoriteCount'])),
          comment_count: parseCount(pick(value, ['commentCount', 'realCommentCount'])),
        });
      }
    }
    for (const child of Object.values(value)) walk(child, depth + 1);
  };
  walk(state);

  // Current profile cards may expose only a cover clientCacheKey, not an href.
  for (const image of Array.from(document.querySelectorAll('img')).slice(0, 300)) {
    const cover = image.currentSrc || image.src || image.getAttribute('data-src') || '';
    const photoId = idFromCover(cover);
    if (!photoId) continue;
    const card = image.closest('.photo-card, .video-card, [class*="photo-card"], [class*="video-card"], [class*="card"], li') || image.parentElement || image;
    const titleEl = card.querySelector('.caption, .video-info-title, [class*="caption"], [class*="title"]');
    const likeEl = card.querySelector('[data-like-count], .like-count, [class*="like-count"], [class*="like"] [class*="count"]');
    const title = (titleEl ? titleEl.innerText || titleEl.textContent || '' : image.alt || '').replace(/\s+/g, ' ').trim();
    merge({
      photo_id: photoId,
      caption: title.split('#')[0].trim(),
      tags: (title.match(/#[\u4e00-\u9fa5\w]+/g) || []).map(tag => tag.slice(1)),
      author: profileAuthor,
      cover,
      like_count: parseCount(likeEl ? (likeEl.getAttribute('data-like-count') || likeEl.innerText || likeEl.textContent || '') : ''),
    });
  }

  // Older layouts still render actual links.
  for (const anchor of Array.from(document.querySelectorAll('a[href*="/short-video/"]')).slice(0, 150)) {
    const href = anchor.href || anchor.getAttribute('href') || '';
    const match = href.match(/\/short-video\/([^/?#]+)/);
    if (!match) continue;
    const photoId = normalizeId(match[1]);
    const card = anchor.closest('.photo-card, .video-card, [class*="photo-card"], [class*="video-card"], [class*="card"], li') || anchor;
    const image = card.querySelector('img');
    const titleEl = card.querySelector('.caption, .video-info-title, [class*="caption"], [class*="title"]');
    const likeEl = card.querySelector('[data-like-count], .like-count, [class*="like-count"], [class*="like"] [class*="count"]');
    const title = (titleEl ? titleEl.innerText || titleEl.textContent || '' : image?.alt || '').replace(/\s+/g, ' ').trim();
    merge({
      photo_id: photoId,
      caption: title.split('#')[0].trim(),
      tags: (title.match(/#[\u4e00-\u9fa5\w]+/g) || []).map(tag => tag.slice(1)),
      author: profileAuthor,
      cover: image ? (image.currentSrc || image.src || '') : '',
      like_count: parseCount(likeEl ? (likeEl.getAttribute('data-like-count') || likeEl.innerText || likeEl.textContent || '') : ''),
    });
  }
  return {profile_author: profileAuthor, profile_user_id: target, items: Array.from(results.values())};
}
"""


# Caption/realLikeCount/viewCount/coverUrl/photoUrl/timestamp are present in
# window.__APOLLO_STATE__. DOM is used both as a primary source for stable named
# selectors and as a fallback. Comment-row count is intentionally never used as
# a total comment count.
_EXTRACT_DETAIL_JS = r"""
(targetPhotoId) => {
  const normalizeId = (value) => decodeURIComponent(String(value || '')).replace(/_ccc$/, '');
  const target = normalizeId(targetPhotoId);
  const own = (obj, key) => !!obj && typeof obj === 'object' && Object.prototype.hasOwnProperty.call(obj, key);
  const parseCount = (raw) => {
    const text = String(raw ?? '').replace(/,/g, '').trim();
    if (!text) return null;
    const match = text.match(/([\d.]+)\s*([万亿wk]?)/i);
    if (!match) return null;
    const unit = match[2].toLowerCase();
    const factor = unit === '亿' ? 100000000 : (unit === '万' || unit === 'w') ? 10000 : unit === 'k' ? 1000 : 1;
    const n = Number.parseFloat(match[1]);
    return Number.isFinite(n) ? Math.round(n * factor) : null;
  };
  const pick = (obj, keys) => {
    for (const key of keys) if (own(obj, key)) return obj[key];
    return undefined;
  };
  const pickUrl = (value, depth = 0) => {
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
      for (const key of ['url', 'urlDefault', 'coverUrl', 'photoUrl', 'cdn']) {
        if (typeof value[key] === 'string' && value[key]) return value[key];
      }
      for (const child of Object.values(value)) {
        const found = pickUrl(child, depth + 1);
        if (found) return found;
      }
    }
    return '';
  };
  const state = window.__APOLLO_STATE__ || {};
  const deref = (value) => value && value.__ref && state[value.__ref] ? state[value.__ref] : value;
  const candidates = [];
  const seen = new WeakSet();
  const visit = (value, path, depth) => {
    if (!value || typeof value !== 'object' || seen.has(value) || depth > 8) return;
    seen.add(value);
    if (!Array.isArray(value)) {
      const id = normalizeId(pick(value, ['photoId', 'photo_id', 'clientCacheKey', 'id']) || '');
      let score = 0;
      if (id && id === target) score += 100;
      if (own(value, 'caption')) score += 8;
      if (own(value, 'realLikeCount') || own(value, 'likeCount')) score += 4;
      if (own(value, 'viewCount') || own(value, 'playCount')) score += 4;
      if (own(value, 'coverUrl') || own(value, 'photoUrl')) score += 3;
      if (path.includes(target)) score += 50;
      if (score > 0) candidates.push({value, score});
    }
    for (const [key, child] of Object.entries(value)) visit(child, path + '.' + key, depth + 1);
  };
  visit(state, '', 0);
  candidates.sort((a, b) => b.score - a.score);
  const apollo = candidates.length ? candidates[0].value : {};
  const nestedPhoto = deref(apollo.photo || apollo.visionPhoto || apollo.currentPhoto) || {};
  const photo = (nestedPhoto && typeof nestedPhoto === 'object' && Object.keys(nestedPhoto).length) ? nestedPhoto : apollo;
  const authorObject = deref(photo.author || photo.user || photo.owner || photo.userInfo || apollo.author || apollo.user) || {};

  const textOf = (selector) => {
    const element = document.querySelector(selector);
    return element ? (element.innerText || element.textContent || '').trim() : '';
  };
  const backgroundUrl = (element) => {
    if (!element) return '';
    const value = element.style.backgroundImage || getComputedStyle(element).backgroundImage || '';
    const match = value.match(/url\(["']?(.*?)["']?\)/);
    return match ? match[1] : '';
  };
  const metricFromInteractive = (kind) => {
    const definitions = {
      like: {classes: ['like-item'], words: ['点赞']},
      collect: {classes: ['collect-item', 'favorite-item'], words: ['收藏']},
      comment: {classes: ['comment-action-item'], words: ['评论']},
    };
    const definition = definitions[kind];
    for (const item of document.querySelectorAll('.interactive-item')) {
      const signature = `${item.className || ''} ${item.getAttribute('title') || ''} ${item.getAttribute('aria-label') || ''}`;
      const semantic = definition.classes.some(name => signature.includes(name))
        || definition.words.some(word => signature.includes(word));
      if (!semantic) continue;
      const count = item.querySelector('.item-text.item-count') || item;
      return parseCount(count.innerText || count.textContent || '');
    }
    return null;
  };
  const domTitle = textOf('.video-info-title');
  const domAuthor = (textOf('.profile-user-name-title') || textOf('.profile-user-name')).split('\n')[0].trim();
  const cover = pickUrl(pick(photo, ['coverUrl', 'coverUrls', 'cover']))
    || backgroundUrl(document.querySelector('.backimg-area'))
    || document.querySelector('video')?.poster || '';
  const title = domTitle || String(pick(photo, ['caption', 'title', 'description']) || '');
  const author = domAuthor || String(pick(authorObject, ['name', 'userName', 'nickname']) || '');
  const profileAnchor = document.querySelector('.profile-user-name[href*="/profile/"], .profile-user-name-title[href*="/profile/"], a[href*="/profile/"]');
  const profileMatch = profileAnchor && (profileAnchor.href || '').match(/\/profile\/([^/?#]+)/);
  const userId = String(pick(authorObject, ['id', 'userId', 'user_id', 'kwaiId']) || (profileMatch ? profileMatch[1] : ''));

  const apolloLike = pick(photo, ['realLikeCount', 'likeCount', 'likedCount']);
  const apolloView = pick(photo, ['viewCount', 'realViewCount', 'playCount']);
  const apolloCollect = pick(photo, ['collectCount', 'collectedCount', 'favoriteCount', 'realCollectCount']);
  const apolloComment = pick(photo, ['commentCount', 'realCommentCount']);
  const likeCount = apolloLike !== undefined ? parseCount(apolloLike) : metricFromInteractive('like');
  const viewCount = apolloView !== undefined ? parseCount(apolloView) : null;
  // Only semantically labelled interactive totals are accepted. Loaded
  // .comment-item.comment-list-item rows are never counted as a total.
  const collectCount = apolloCollect !== undefined ? parseCount(apolloCollect) : metricFromInteractive('collect');
  const commentCount = apolloComment !== undefined ? parseCount(apolloComment) : metricFromInteractive('comment');
  const timestamp = parseCount(pick(photo, ['timestamp', 'createTime', 'publishTime']));
  const resolvedId = normalizeId(pick(photo, ['photoId', 'photo_id', 'clientCacheKey', 'id']) || target);
  const mediaUrl = pickUrl(pick(photo, ['photoUrl', 'playUrl', 'videoUrl']));
  return {
    found: !!(resolvedId && (title || author || cover || candidates.length)),
    photo_id: resolvedId,
    title,
    description: title,
    author,
    user_id: userId,
    cover,
    photo_url: mediaUrl,
    publish_time: timestamp,
    view_count: viewCount,
    liked_count: likeCount,
    collected_count: collectCount,
    comment_count: commentCount,
    url: location.href,
  };
}
"""


_COMMENT_TOTAL_JS = r"""
async (targetPhotoId) => {
  const photoId = String(targetPhotoId || '').replace(/_ccc$/, '');
  const query = `
    query commentListQuery($photoId: String, $pcursor: String) {
      visionCommentList(photoId: $photoId, pcursor: $pcursor) {
        commentCountV2
        pcursorV2
      }
    }
  `;
  try {
    const response = await fetch('/graphql', {
      method: 'POST',
      credentials: 'include',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({
        operationName: 'commentListQuery',
        variables: {photoId, pcursor: ''},
        query,
      }),
    });
    const payload = await response.json();
    const result = payload && payload.data && payload.data.visionCommentList;
    return {
      ok: response.ok,
      status: response.status,
      comment_count_v2: result ? result.commentCountV2 : null,
      pcursor_v2: result ? result.pcursorV2 : null,
    };
  } catch (error) {
    return {ok: false, status: 0, comment_count_v2: null, error: String(error)};
  }
}
"""


async def _check_login_or_raise() -> None:
    cookies = await pool.get_cookies("kuaishou")
    if not pool.is_logged_in("kuaishou", cookies):
        raise ScraperError(
            "kuaishou requires login. Call POST /api/browser/login/kuaishou first to authenticate.",
            code=ErrorCode.CREDENTIAL_EXPIRED,
            status_code=401,
            retryable=False,
            capability="browser-login",
        )


class KuaishouScraper(BaseScraper):
    platform = "kuaishou"
    degrades_to_mock = False

    @staticmethod
    def _search_result(raw: dict) -> dict:
        photo_id = _normalize_photo_id(raw.get("photo_id"))
        return {
            "content_id": photo_id,
            "title": (raw.get("caption") or "")[:120],
            "author": raw.get("author") or "",
            "url": _DETAIL_URL.format(photo_id=photo_id) if photo_id else "",
            "stats": {
                "view": _optional_int(raw.get("view_count")),
                "like": _optional_int(raw.get("like_count")),
                "collect": _optional_int(raw.get("collect_count")),
                "reply": _optional_int(raw.get("comment_count")),
                "share": _optional_int(raw.get("share_count")),
            },
            "tags": raw.get("tags") or [],
            "cover": raw.get("cover") or "",
            "photo_id": photo_id,
        }

    async def search(self, keyword: str, limit: int = 20) -> list[dict]:
        await _check_login_or_raise()
        url = _SEARCH_URL.format(keyword=quote(str(keyword or ""), safe=""))
        await pool.navigate("kuaishou", url, wait_until="domcontentloaded", timeout_ms=20000)
        try:
            await pool.wait_for_state("kuaishou", _WAIT_JS, timeout_ms=10000)
        except ScraperError:
            logger.warning("kuaishou search cards not ready: %s", keyword)
        await asyncio_sleep(1.5)
        items = await pool.evaluate("kuaishou", _EXTRACT_SEARCH_JS, timeout_ms=10000)
        if not isinstance(items, list):
            return []
        return [self._search_result(item) for item in items[: max(0, limit)] if _normalize_photo_id(item.get("photo_id"))]

    async def get_user_contents(
        self,
        identifier: str,
        *,
        account_name: str = "",
        limit: int = 20,
    ) -> list[dict]:
        await _check_login_or_raise()
        user_id, profile_url = _profile_from_identifier(identifier)
        if not user_id:
            if not account_name:
                raise ScraperError(
                    "kuaishou account sync requires a profile URL/native ID or account_name",
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

        await pool.navigate("kuaishou", profile_url, wait_until="domcontentloaded", timeout_ms=20000)
        try:
            await pool.wait_for_state(
                "kuaishou",
                "() => document.querySelectorAll('a[href*=\"/short-video/\"]').length > 0",
                timeout_ms=12000,
            )
        except ScraperError:
            logger.warning("kuaishou profile items not ready: %s", user_id)
        raw = await pool.evaluate("kuaishou", _EXTRACT_PROFILE_JS, user_id, timeout_ms=12000)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            return []
        profile_author = raw.get("profile_author") or account_name
        results: list[dict] = []
        seen: set[str] = set()
        for item in raw["items"]:
            photo_id = _normalize_photo_id(item.get("photo_id"))
            if not photo_id or photo_id in seen:
                continue
            seen.add(photo_id)
            item["photo_id"] = photo_id
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
        photo_id = _normalize_photo_id(content_id)
        if not photo_id:
            raise ScraperError(
                f"invalid kuaishou photo id: {content_id!r}",
                code=ErrorCode.INVALID_CONTENT_ID,
                status_code=400,
                retryable=False,
                capability="info",
            )
        url = _DETAIL_URL.format(photo_id=quote(photo_id, safe=""))

        async def load_detail() -> dict:
            await pool.navigate("kuaishou", url, wait_until="domcontentloaded", timeout_ms=20000)
            try:
                await pool.wait_for_state(
                    "kuaishou",
                    "() => !!document.querySelector('.video-info-title') && !!document.querySelector('.interactive-item.like-item')",
                    timeout_ms=8000,
                )
            except ScraperError:
                logger.warning("kuaishou detail fields not ready before extraction: %s", photo_id)
            await asyncio_sleep(0.3)
            result = await pool.evaluate("kuaishou", _EXTRACT_DETAIL_JS, photo_id, timeout_ms=15000)
            return result if isinstance(result, dict) else {}

        value: dict[str, Any] = {}
        for attempt in range(2):
            value = await load_detail()
            if value.get("found") and str(value.get("title") or "").strip():
                break
            if attempt == 0:
                logger.warning("kuaishou detail missing title; retrying once: %s", photo_id)
                await asyncio_sleep(1.0)

        comment_total: dict[str, Any] = {}
        try:
            comment_total = await pool.evaluate("kuaishou", _COMMENT_TOTAL_JS, photo_id, timeout_ms=15000)
        except ScraperError as exc:
            logger.warning("kuaishou official comment total unavailable for %s: %s", photo_id, exc)
        if not value or not value.get("found"):
            raise ScraperError(
                f"kuaishou info not found: {content_id}",
                code=ErrorCode.CONTENT_NOT_FOUND,
                status_code=404,
                retryable=False,
                capability="info",
            )
        resolved_id = _normalize_photo_id(value.get("photo_id")) or photo_id
        cover = value.get("cover") or ""
        metrics = {
            "view_count": _optional_int(value.get("view_count")),
            "play_count": _optional_int(value.get("view_count")),
            "liked_count": _optional_int(value.get("liked_count")),
            "collected_count": _optional_int(value.get("collected_count")),
            "comment_count": _optional_int(comment_total.get("comment_count_v2")),
            "share_count": None,
        }
        return {
            "content_id": resolved_id,
            "title": (value.get("title") or "")[:240],
            "description": (value.get("description") or value.get("title") or "")[:2000],
            "user": {
                "nickname": value.get("author") or "",
                "user_id": value.get("user_id") or None,
                "profile_url": _PROFILE_URL.format(user_id=quote(str(value.get("user_id") or ""), safe="")) if value.get("user_id") else None,
            },
            "cover": cover,
            "images": [cover] if cover else [],
            "media_type": "video",
            "url": value.get("url") or _DETAIL_URL.format(photo_id=resolved_id),
            "publish_time": _optional_int(value.get("publish_time")),
            "interact_info": metrics,
            "metric_presence": {key: metric is not None for key, metric in metrics.items()},
            "metric_sources": {
                "comment_count": "visionCommentList.commentCountV2" if metrics["comment_count"] is not None else None,
                "collected_count": None if metrics["collected_count"] is None else "public_detail",
            },
        }

    async def get_comments(self, content_id: str, max_comments: int = 20, **kwargs) -> list[dict]:
        photo_id = _normalize_photo_id(content_id)
        reuse_current_page = bool(kwargs.pop("reuse_current_page", False))
        page_ready = False
        if reuse_current_page and photo_id:
            current = await pool.evaluate(
                "kuaishou",
                "() => ({url: location.href, title: (document.querySelector('.video-info-title')?.innerText || '').trim()})",
                timeout_ms=5000,
            )
            if isinstance(current, dict):
                page_ready = _normalize_photo_id(current.get("url")) == photo_id and bool(str(current.get("title") or "").strip())
        if not page_ready:
            await self.get_info(photo_id or content_id)
        target_count = max(1, min(int(max_comments), 8))
        try:
            await pool.wait_for_state(
                "kuaishou",
                f"() => document.querySelectorAll('.comment-item.comment-list-item').length >= {target_count}",
                timeout_ms=6000,
            )
        except ScraperError:
            logger.warning("kuaishou rendered fewer than %s comments before extraction", target_count)
        comments_js = r"""
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
  return Array.from(document.querySelectorAll('.comment-item.comment-list-item')).slice(0, 50).map(el => {
    const contentElement = el.querySelector('.comment-item-content, .content, .text, [class*="comment-content"]');
    const contentText = contentElement ? (contentElement.innerText || contentElement.textContent || '').trim() : '';
    const emojiText = contentElement
      ? Array.from(contentElement.querySelectorAll('img[alt]')).map(image => image.getAttribute('alt') || '').join('')
      : '';
    return {
      nickname: (el.querySelector('.author-name, .user-name, .name, [class*="user-name"]') || {}).innerText || '',
      content: `${contentText}${emojiText}`.trim(),
      like_count: parseCount((el.querySelector('.comment-item-operation-op.likeop, .likeop') || {}).innerText || ''),
      create_time: ((el.querySelector('.comment-item-time, [class*="comment-time"]') || {}).innerText || '').trim(),
    };
  });
}
"""
        comments = await pool.evaluate("kuaishou", comments_js, timeout_ms=10000)
        if not isinstance(comments, list):
            return []
        return [
            {
                "user_info": {"nickname": item.get("nickname") or ""},
                "content": item.get("content") or "",
                "like_count": _optional_int(item.get("like_count")),
                "create_time": None,
                "create_time_text": item.get("create_time") or "",
                "sub_comments": [],
            }
            for item in comments[:max_comments]
            if item.get("content") or item.get("nickname")
        ]


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
