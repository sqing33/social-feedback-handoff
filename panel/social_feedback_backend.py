"""内容生成归档与社媒反馈面板后端骨架

目标：
- 给现成前端页面提供稳定 API
- 先用 SQLite 落地归档 / 反馈 / 收藏 / 删除
- 未来 CLI 工具只需要往 /api/ingest /api/archives/<id>/feedback 写数据

运行：
    python social_feedback_backend.py

默认：
- 前端页面：./social-feedback-panel.html
- 数据库：./social_feedback.db
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import ssl
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from flask import Flask, abort, jsonify, request, send_file

from ai_service import (
    enqueue_job as enqueue_ai_job,
    load_config as load_ai_config,
    normalize_topic,
)

logger = logging.getLogger("social-feedback-panel")

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "social_feedback.db"
FRONTEND_PATH = APP_DIR / "social-feedback-panel.html"

PRIMARY_SYNC_PLATFORMS = ["小红书", "抖音", "快手", "B站"]
PUBLIC_ONLY_PLATFORMS = ["微信公众号"]
PUBLIC_ONLY_SET = set(PUBLIC_ONLY_PLATFORMS)
PLATFORMS = [*PRIMARY_SYNC_PLATFORMS, *PUBLIC_ONLY_PLATFORMS]
ACCOUNT_SYNC_PLATFORMS = set(PRIMARY_SYNC_PLATFORMS)
ASSET_KINDS = {"cover", "image"}
ANALYTICS_METRIC_FIELDS = ("views", "likes", "saves", "comments", "shares")
ENGAGEMENT_METRIC_FIELDS = ("likes", "saves", "comments", "shares")

FETCH_INTERVAL_PLATFORMS = {
    "xhs": "小红书",
    "douyin": "抖音",
    "kuaishou": "快手",
    "bilibili": "B站",
}
PLATFORM_FETCH_POLICY = {
    platform: {
        "min_interval_seconds": 5.0,
        "max_interval_seconds": 10.0,
        "retry_backoff_seconds": (18.0, 32.0),
        "max_retries": 1,
        "timeout_seconds": 18,
    }
    for platform in FETCH_INTERVAL_PLATFORMS
}
# Keep this public name for existing callers/tests while making the policy
# shared by all four real scraper-backed platforms.
XIAOHONGSHU_FETCH_POLICY = PLATFORM_FETCH_POLICY["xhs"]
STRICT_CAPTURE_PLATFORMS = {"小红书", "抖音", "快手"}
REQUIRED_CAPTURE_FIELDS = {"title"}
FETCH_STATE = {
    "last_xiaohongshu_fetch_at": 0.0,
    **{f"last_{platform}_fetch_at": 0.0 for platform in FETCH_INTERVAL_PLATFORMS if platform != "xhs"},
}
FETCH_STATE_KEYS = {
    "xhs": "last_xiaohongshu_fetch_at",
    **{platform: f"last_{platform}_fetch_at" for platform in FETCH_INTERVAL_PLATFORMS if platform != "xhs"},
}
PLATFORM_FETCH_LOCKS = {platform: threading.Lock() for platform in FETCH_INTERVAL_PLATFORMS}

SCRAPER_SERVICE_URL = os.getenv("SCRAPER_URL", "http://127.0.0.1:8007").rstrip("/")
SCRAPER_SERVICE_API_KEY = os.getenv("SCRAPER_API_KEY", "").strip()
SCRAPER_SERVICE_TIMEOUT = max(5, int(os.getenv("SCRAPER_TIMEOUT_SECONDS", "75")))
SCRAPER_PLATFORM_MAP = {
    "小红书": "xhs",
    "B站": "bilibili",
    "抖音": "douyin",
    "快手": "kuaishou",
    "Twitter/X": "twitter",
    "YouTube": "youtube",
    "TikTok": "tiktok",
}
SCRAPER_SERVICE_PLATFORMS = set(SCRAPER_PLATFORM_MAP.values())
LOGIN_PLATFORM_DEFINITIONS = [
    {"key": "xhs", "name": "小红书"},
    {"key": "douyin", "name": "抖音"},
    {"key": "kuaishou", "name": "快手"},
    {"key": "bilibili", "name": "B站"},
]
LOGIN_PLATFORM_KEYS = {item["key"] for item in LOGIN_PLATFORM_DEFINITIONS}

app = Flask(__name__)

# Formal mode is strict by default: only successful real scraper captures may
# create or mutate platform records. Legacy direct-write APIs can be enabled
# explicitly for a controlled migration, never accidentally in production.
ALLOW_LEGACY_WRITE_API = os.getenv("ALLOW_LEGACY_WRITE_API", "0").lower() in {"1", "true", "yes", "on"}
FORMAL_MODE = os.getenv("FORMAL_MODE", "1").lower() in {"1", "true", "yes", "on"}
ALLOW_GENERIC_PUBLIC_CAPTURE = os.getenv("ALLOW_GENERIC_PUBLIC_CAPTURE", "0").lower() in {"1", "true", "yes", "on"}
PANEL_ALLOWED_ORIGIN = os.getenv("PANEL_ALLOWED_ORIGIN", "").strip()


def legacy_write_guard():
    if ALLOW_LEGACY_WRITE_API:
        return None
    return jsonify({
        "error": "正式模式已禁用直接写入接口，请使用真实笔记链接导入或刷新",
        "code": "LEGACY_WRITE_DISABLED",
    }), 410


# ----------------------------
# DB
# ----------------------------
def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------
# Link capture helpers
# ----------------------------
CONNECTOR_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "key": "xiaohongshu",
        "platform": "小红书",
        "name": "小红书连接器",
        "domains": ["xiaohongshu.com", "xhslink.com", "xhs.cn"],
        "match_terms": ["xiaohongshu", "xhslink", "xhs.cn"],
        "capabilities": ["link_capture", "asset_extract", "account_sync_ready"],
        "sync_scope": "account_full",
        "sync_label": "账号全量同步",
        "sync_note": "小红书优先走账号授权 / 登录态，公开页只作补充。",
        "description": "面向小红书笔记链接的统一抓取入口。",
    },
    {
        "key": "douyin",
        "platform": "抖音",
        "name": "抖音连接器",
        "domains": ["douyin.com", "iesdouyin.com"],
        "match_terms": ["douyin", "iesdouyin"],
        "capabilities": ["link_capture", "asset_extract", "account_sync_ready"],
        "sync_scope": "account_full",
        "sync_label": "账号全量同步",
        "sync_note": "抖音优先走账号授权 / 登录态，公开页只作补充。",
        "description": "面向抖音作品链接的统一抓取入口。",
    },
    {
        "key": "kuaishou",
        "platform": "快手",
        "name": "快手连接器",
        "domains": ["kuaishou.com", "gifshow.com", "ks.cn"],
        "match_terms": ["kuaishou", "gifshow", "ks.cn"],
        "capabilities": ["link_capture", "asset_extract", "account_sync_ready"],
        "sync_scope": "account_full",
        "sync_label": "账号全量同步",
        "sync_note": "快手优先走账号授权 / 登录态，公开页不作为完整数据源。",
        "description": "面向快手作品链接的统一抓取入口。",
    },
    {
        "key": "bilibili",
        "platform": "B站",
        "name": "B站连接器",
        "domains": ["bilibili.com", "b23.tv"],
        "match_terms": ["bilibili", "b23.tv", "bili"],
        "capabilities": ["link_capture", "asset_extract", "account_sync_ready"],
        "sync_scope": "account_full",
        "sync_label": "账号全量同步",
        "sync_note": "B站优先走账号授权 / 登录态，公开页可先做补充采集。",
        "description": "面向 B 站视频链接的统一抓取入口。",
    },
    {
        "key": "wechat",
        "platform": "微信公众号",
        "name": "微信公众号连接器",
        "domains": ["mp.weixin.qq.com", "weixin.qq.com"],
        "match_terms": ["mp.weixin.qq.com", "weixin.qq.com", "weixin"],
        "capabilities": ["link_capture", "asset_extract", "account_sync_ready"],
        "sync_scope": "public_only",
        "sync_label": "仅公开抓取",
        "sync_note": "公众号只抓公开文章、公开主页和搜索结果，不碰后台。",
        "description": "面向公众号文章链接的统一抓取入口。",
    },
]
GENERIC_CONNECTOR: Dict[str, Any] = {
    "key": "generic",
    "platform": "通用网页",
    "name": "通用网页连接器",
    "domains": [],
    "match_terms": [],
    "capabilities": ["link_capture"],
    "sync_scope": "public_only",
    "sync_label": "仅公开抓取",
    "sync_note": "未识别平台先按公开信息处理。",
    "description": "无法识别平台时的兜底连接器。",
}

def connector_sync_policy(platform: str) -> Dict[str, str]:
    platform = (platform or "").strip()
    if platform in PUBLIC_ONLY_SET:
        return {
            "sync_scope": "public_only",
            "sync_label": "仅公开抓取",
            "sync_note": "公众号只抓公开信息，不碰后台。",
        }
    if platform in ACCOUNT_SYNC_PLATFORMS:
        return {
            "sync_scope": "account_full",
            "sync_label": "账号全量同步",
            "sync_note": "优先走账号授权 / 登录态，尽量补齐完整数据。",
        }
    return {
        "sync_scope": "public_only",
        "sync_label": "仅公开抓取",
        "sync_note": "未识别平台先按公开信息处理。",
    }


def connector_signature(url: str) -> str:
    parsed = urlparse(url)
    return f"{(parsed.netloc or '').lower()}{(parsed.path or '').lower()}"


def connector_host_allowed(url: str, connector: Dict[str, Any]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    domains = [str(item or "").lower().lstrip(".").rstrip(".") for item in connector.get("domains", [])]
    return bool(host and any(host == domain or host.endswith(f".{domain}") for domain in domains if domain))


def validate_formal_capture_url(url: str, connector: Dict[str, Any]) -> None:
    if not FORMAL_MODE:
        return
    if connector.get("key") == "generic" and not ALLOW_GENERIC_PUBLIC_CAPTURE:
        raise ValueError("正式模式只允许已支持平台的公开链接")
    if connector.get("domains") and not connector_host_allowed(url, connector):
        raise ValueError("链接域名与所选平台不匹配")


def resolve_connector(url: str, platform_hint: str = "") -> Dict[str, Any]:
    signature = connector_signature(url)
    if platform_hint:
        for spec in CONNECTOR_DEFINITIONS:
            if spec["platform"] == platform_hint:
                connector = dict(spec)
                connector["matched_by"] = "platform_hint"
                return connector
    for spec in CONNECTOR_DEFINITIONS:
        if any(needle in signature for needle in spec["match_terms"]):
            connector = dict(spec)
            connector["matched_by"] = "url"
            return connector
    connector = dict(GENERIC_CONNECTOR)
    connector["platform"] = platform_hint or connector["platform"]
    connector["matched_by"] = "generic"
    return connector


def list_connectors() -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    for spec in [*CONNECTOR_DEFINITIONS, GENERIC_CONNECTOR]:
        catalog.append({
            "key": spec["key"],
            "platform": spec["platform"],
            "name": spec["name"],
            "domains": spec["domains"],
            "capabilities": spec["capabilities"],
            "sync_scope": spec.get("sync_scope", connector_sync_policy(spec["platform"])["sync_scope"]),
            "sync_label": spec.get("sync_label", connector_sync_policy(spec["platform"])["sync_label"]),
            "sync_note": spec.get("sync_note", connector_sync_policy(spec["platform"])["sync_note"]),
            "description": spec["description"],
            "status": "ready",
        })
    return catalog


def detect_platform_from_url(url: str) -> str:
    return resolve_connector(url).get("platform", "")


def safe_json_loads(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def parse_count_text(value: str) -> int:
    raw = normalize_text(value).replace(",", "")
    if not raw:
        return 0
    multiplier = 1
    lower = raw.lower()
    if lower.endswith(("万", "w")):
        multiplier = 10000
        raw = raw[:-1]
    elif lower.endswith("k"):
        multiplier = 1000
        raw = raw[:-1]
    raw = re.sub(r"[^0-9.]+", "", raw)
    if not raw:
        return 0
    try:
        return int(float(raw) * multiplier)
    except ValueError:
        return 0


def required_capture_missing_fields(snapshot: Dict[str, Any]) -> List[str]:
    platform = (snapshot.get("platform") or "").strip()
    if platform not in STRICT_CAPTURE_PLATFORMS:
        return []
    return [] if (snapshot.get("title") or "").strip() else ["title"]


def mark_snapshot_if_incomplete(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    missing = required_capture_missing_fields(snapshot)
    if missing:
        snapshot["status"] = "incomplete"
        snapshot["missing_fields"] = missing
        snapshot["error"] = "强制字段缺失，未进入正式归档：" + "、".join(missing)
    return snapshot


def fetch_interval_platform_key(platform: str) -> str:
    raw = str(platform or "").strip()
    if raw in FETCH_INTERVAL_PLATFORMS:
        return raw
    for key, name in FETCH_INTERVAL_PLATFORMS.items():
        if raw == name:
            return key
    return ""


def platform_fetch_pause(platform: str, attempt: int = 0) -> None:
    platform_key = fetch_interval_platform_key(platform)
    if not platform_key:
        return
    policy = PLATFORM_FETCH_POLICY[platform_key]
    lock = PLATFORM_FETCH_LOCKS[platform_key]
    state_key = FETCH_STATE_KEYS[platform_key]
    with lock:
        last_at = FETCH_STATE.get(state_key, 0.0)
        now = time.time()
        floor = policy["min_interval_seconds"]
        ceiling = policy["max_interval_seconds"]
        desired = random.uniform(floor, ceiling)
        wait_seconds = max(0.0, desired - (now - last_at))
        if attempt > 0:
            backoff_min, backoff_max = policy["retry_backoff_seconds"]
            wait_seconds = max(wait_seconds, random.uniform(backoff_min, backoff_max))
        if wait_seconds:
            time.sleep(wait_seconds)
        FETCH_STATE[state_key] = time.time()


def xiaohongshu_fetch_pause(attempt: int = 0) -> None:
    """Backward-compatible wrapper for the shared platform throttle."""
    platform_fetch_pause("xhs", attempt=attempt)


def fetch_url_text(url: str, timeout: int = 15) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (MiniMax Social Feedback Bot)"})
    context = ssl._create_unverified_context()
    with urlopen(req, timeout=timeout, context=context) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        data = resp.read()
    return data.decode(charset, errors="replace")


def resolve_url(base_url: str, candidate: str) -> str:
    candidate = (candidate or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("//"):
        parsed = urlparse(base_url)
        return f"{parsed.scheme}:{candidate}"
    return urljoin(base_url, candidate)



def is_public_wechat_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if "mp.weixin.qq.com" not in host and "weixin.qq.com" not in host:
        return True
    blocked_fragments = ["/cgi-bin/login", "/cgi-bin/home", "/cgi-bin/account", "/cgi-bin/appmsg", "/misc/"]
    if any(fragment in path for fragment in blocked_fragments):
        return False
    public_article_flags = [
        path.startswith("/s"),
        "__biz=" in query and ("mid=" in query or "idx=" in query),
        "weixin.qq.com/s" in host + path,
    ]
    return any(public_article_flags)



def extract_metric_map(text: str, pattern_map: Dict[str, List[str]]) -> Dict[str, int]:
    metrics = {key: 0 for key in pattern_map}
    for key, patterns in pattern_map.items():
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                metrics[key] = parse_count_text(match.group(1))
                break
    return metrics


def extract_bilibili_snapshot(html_text: str, url: str, base_meta: Dict[str, Any]) -> Dict[str, Any]:
    text = html_text or ""
    title = base_meta.get("title") or ""
    author = base_meta.get("author") or ""
    metric_patterns = {
        "views": [r'"view"\s*:\s*([0-9]+)', r'"play"\s*:\s*([0-9]+)'],
        "likes": [r'"like"\s*:\s*([0-9]+)', r'"digg"\s*:\s*([0-9]+)'],
        "saves": [r'"favorite"\s*:\s*([0-9]+)', r'"collect"\s*:\s*([0-9]+)'],
        "comments": [r'"reply"\s*:\s*([0-9]+)', r'"comment"\s*:\s*([0-9]+)'],
    }
    extra_metrics = {
        "coins": 0,
        "shares": 0,
        "danmaku": 0,
    }
    for key, patterns in {
        "coins": [r'"coin"\s*:\s*([0-9]+)'],
        "shares": [r'"share"\s*:\s*([0-9]+)'],
        "danmaku": [r'"danmaku"\s*:\s*([0-9]+)'],
    }.items():
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                extra_metrics[key] = int(match.group(1))
                break
    metrics = extract_metric_map(text, metric_patterns)
    if not title:
        title_match = re.search(r'"title"\s*:\s*"([^"]{2,200})"', text)
        if title_match:
            title = normalize_text(title_match.group(1))
    if not author:
        owner_match = re.search(r'"owner"\s*:\s*\{.*?"name"\s*:\s*"([^"]{1,80})"', text, re.S)
        if owner_match:
            author = normalize_text(owner_match.group(1))
    return {
        "title": title,
        "author": author,
        "metrics": metrics,
        "extra_metrics": extra_metrics,
    }


def extract_xiaohongshu_snapshot(html_text: str, base_meta: Dict[str, Any]) -> Dict[str, Any]:
    text = html_text or ""
    title = base_meta.get("title") or ""
    author = base_meta.get("author") or ""
    metric_patterns = {
        "views": [r'"viewCount"\s*:\s*([0-9]+)', r'"view"\s*:\s*([0-9]+)'],
        "likes": [r'"likeCount"\s*:\s*([0-9]+)', r'"likedCount"\s*:\s*([0-9]+)'],
        "saves": [r'"collectCount"\s*:\s*([0-9]+)', r'"favoriteCount"\s*:\s*([0-9]+)'],
        "comments": [r'"commentCount"\s*:\s*([0-9]+)'],
    }
    if not title:
        title_match = re.search(r'"title"\s*:\s*"([^"]{2,200})"', text)
        if title_match:
            title = normalize_text(title_match.group(1))
    if not author:
        author_match = re.search(r'"nickname"\s*:\s*"([^"]{1,80})"', text)
        if author_match:
            author = normalize_text(author_match.group(1))
    metrics = extract_metric_map(text, metric_patterns)
    return {
        "title": title,
        "author": author,
        "metrics": metrics,
        "extra_metrics": {},
    }


def extract_douyin_snapshot(html_text: str, base_meta: Dict[str, Any]) -> Dict[str, Any]:
    text = html_text or ""
    title = base_meta.get("title") or ""
    author = base_meta.get("author") or ""
    metric_patterns = {
        "views": [r'"playCount"\s*:\s*([0-9]+)', r'"play_count"\s*:\s*([0-9]+)'],
        "likes": [r'"diggCount"\s*:\s*([0-9]+)', r'"likeCount"\s*:\s*([0-9]+)'],
        "saves": [r'"collectCount"\s*:\s*([0-9]+)', r'"favoriteCount"\s*:\s*([0-9]+)'],
        "comments": [r'"commentCount"\s*:\s*([0-9]+)'],
    }
    if not title:
        title_match = re.search(r'"desc"\s*:\s*"([^"]{2,200})"', text)
        if title_match:
            title = normalize_text(title_match.group(1))
    if not author:
        author_match = re.search(r'"nickname"\s*:\s*"([^"]{1,80})"', text)
        if author_match:
            author = normalize_text(author_match.group(1))
    metrics = extract_metric_map(text, metric_patterns)
    return {
        "title": title,
        "author": author,
        "metrics": metrics,
        "extra_metrics": {},
    }


def extract_kuaishou_snapshot(html_text: str, base_meta: Dict[str, Any]) -> Dict[str, Any]:
    text = html_text or ""
    title = base_meta.get("title") or ""
    author = base_meta.get("author") or ""
    metric_patterns = {
        "views": [r'"playCount"\s*:\s*([0-9]+)', r'"play_count"\s*:\s*([0-9]+)'],
        "likes": [r'"likeCount"\s*:\s*([0-9]+)', r'"diggCount"\s*:\s*([0-9]+)'],
        "saves": [r'"collectCount"\s*:\s*([0-9]+)', r'"bookmarkCount"\s*:\s*([0-9]+)'],
        "comments": [r'"commentCount"\s*:\s*([0-9]+)'],
    }
    if not title:
        title_match = re.search(r'"caption"\s*:\s*"([^"]{2,200})"', text)
        if title_match:
            title = normalize_text(title_match.group(1))
    if not author:
        author_match = re.search(r'"authorName"\s*:\s*"([^"]{1,80})"', text)
        if author_match:
            author = normalize_text(author_match.group(1))
    metrics = extract_metric_map(text, metric_patterns)
    return {
        "title": title,
        "author": author,
        "metrics": metrics,
        "extra_metrics": {},
    }


def extract_wechat_public_snapshot(html_text: str, base_meta: Dict[str, Any]) -> Dict[str, Any]:
    text = html_text or ""
    title = base_meta.get("title") or ""
    author = base_meta.get("author") or ""
    if not author:
        author_match = re.search(r'var\s+author\s*=\s*["\']([^"\']+)["\']', text, re.I)
        if author_match:
            author = normalize_text(author_match.group(1))
    return {
        "title": title,
        "author": author,
        "metrics": {"views": None, "likes": None, "saves": None, "comments": None, "shares": None},
        "extra_metrics": {},
    }


def build_platform_snapshot(url: str, platform: str, html_text: str, meta: Dict[str, Any], connector_info: Dict[str, Any]) -> Dict[str, Any]:
    if platform == "B站":
        platform_meta = extract_bilibili_snapshot(html_text, url, meta)
    elif platform == "小红书":
        platform_meta = extract_xiaohongshu_snapshot(html_text, meta)
    elif platform == "抖音":
        platform_meta = extract_douyin_snapshot(html_text, meta)
    elif platform == "快手":
        platform_meta = extract_kuaishou_snapshot(html_text, meta)
    elif platform in PUBLIC_ONLY_SET:
        platform_meta = extract_wechat_public_snapshot(html_text, meta)
    else:
        platform_meta = {
            "title": meta.get("title") or "",
            "author": meta.get("author") or "",
            "metrics": extract_social_metrics(html_text),
            "extra_metrics": {},
        }
    metrics = platform_meta.get("metrics") or {"views": None, "likes": None, "saves": None, "comments": None}
    snapshot = {
        "url": url,
        "platform": platform,
        "connector": connector_info,
        "status": "fetched",
        "title": platform_meta.get("title") or meta.get("title") or "",
        "description": meta.get("description") or "",
        "site_name": meta.get("site_name") or "",
        "author": platform_meta.get("author") or meta.get("author") or "",
        "cover": meta.get("images", [""])[0] if meta.get("images") else "",
        "images": meta.get("images", []),
        "metrics": metrics,
        "collection_mode": connector_info["sync_scope"],
        "extra_metrics": platform_meta.get("extra_metrics") or {},
        "error": "",
        "captured_at": now_iso(),
    }
    if not snapshot["platform"]:
        snapshot["platform"] = meta.get("site_name") or connector_info["platform"]
        snapshot["connector"]["platform"] = snapshot["platform"]
    return mark_snapshot_if_incomplete(snapshot)


def extract_metadata(html_text: str, base_url: str) -> Dict[str, Any]:
    text = html_text or ""

    def first_meta(patterns: List[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text, re.I | re.S)
            if match:
                return normalize_text(match.group(1))
        return ""

    title = first_meta([
                r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<meta[^>]+name=[\"']twitter:title[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<title[^>]*>(.*?)</title>",
    ])
    description = first_meta([
                r"<meta[^>]+property=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)[\"']",
    ])
    site_name = first_meta([
                r"<meta[^>]+property=[\"']og:site_name[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<meta[^>]+name=[\"']application-name[\"'][^>]+content=[\"']([^\"']+)[\"']",
    ])
    author = first_meta([
                r"<meta[^>]+name=[\"']author[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<meta[^>]+property=[\"']article:author[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<meta[^>]+name=[\"']twitter:creator[\"'][^>]+content=[\"']([^\"']+)[\"']",
    ])

    image_candidates: List[str] = []
    image_patterns = [
                r"<meta[^>]+property=[\"']og:image(?::url)?[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<meta[^>]+name=[\"']twitter:image(?::src)?[\"'][^>]+content=[\"']([^\"']+)[\"']",
                r"<link[^>]+rel=[\"']image_src[\"'][^>]+href=[\"']([^\"']+)[\"']",
    ]
    for pattern in image_patterns:
        for match in re.finditer(pattern, text, re.I | re.S):
            image_candidates.append(resolve_url(base_url, match.group(1)))

    ld_match = re.search(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", text, re.I | re.S)
    if ld_match:
        data = safe_json_loads(ld_match.group(1).strip(), {})
        if isinstance(data, dict):
            image_value = data.get("image")
            if isinstance(image_value, str):
                image_candidates.append(resolve_url(base_url, image_value))
            elif isinstance(image_value, list):
                image_candidates.extend(resolve_url(base_url, str(item)) for item in image_value if item)

    seen = set()
    images = []
    for item in image_candidates:
        if item and item not in seen:
            seen.add(item)
            images.append(item)

    return {
        "title": title,
        "description": description,
        "site_name": site_name,
        "author": author,
        "images": images[:8],
    }

def extract_social_metrics(html_text: str) -> Dict[str, Optional[int]]:
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text or "", flags=re.I | re.S)
    visible = normalize_text(re.sub(r"<[^>]+>", " ", visible))
    patterns = {
        "views": [r"(?:浏览量|浏览|播放量|播放|阅读量|阅读)\D{0,20}([0-9][0-9,\.]*\s*(?:万|w|k|K)?)"],
        "likes": [r"(?:点赞量|点赞|喜欢)\D{0,20}([0-9][0-9,\.]*\s*(?:万|w|k|K)?)"],
        "saves": [r"(?:收藏量|收藏|转发量|转发)\D{0,20}([0-9][0-9,\.]*\s*(?:万|w|k|K)?)"],
        "comments": [r"(?:评论量|评论)\D{0,20}([0-9][0-9,\.]*\s*(?:万|w|k|K)?)"],
    }
    metrics: Dict[str, Optional[int]] = {"views": None, "likes": None, "saves": None, "comments": None}
    for key, patterns_list in patterns.items():
        for pattern in patterns_list:
            match = re.search(pattern, visible, re.I)
            if match:
                metrics[key] = parse_count_text(match.group(1))
                break
    return metrics


def scraper_platform_key(platform: str) -> str:
    raw = (platform or "").strip()
    if not raw:
        return ""
    if raw in SCRAPER_PLATFORM_MAP:
        return SCRAPER_PLATFORM_MAP[raw]
    lowered = raw.lower()
    if lowered in SCRAPER_SERVICE_PLATFORMS:
        return lowered
    return ""


def nested_value(data: Dict[str, Any] | None, *path: str) -> Any:
    cur: Any = data or {}
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def first_text_value(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = normalize_text(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(int(value)) if float(value).is_integer() else str(value)
        else:
            text = normalize_text(str(value))
        if text:
            return text
    return ""


def first_optional_count_value(*values: Any) -> Optional[int]:
    """Return the first observed metric, preserving absence as ``None``.

    A real metric value of 0 is valid. Strict platform validation therefore
    must not collapse an absent field into 0 before checking completeness.
    """
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            return parse_count_text(text)
        try:
            return int(value)
        except Exception:
            text = normalize_text(str(value))
            if text:
                return parse_count_text(text)
    return None


def first_count_value(*values: Any) -> int:
    value = first_optional_count_value(*values)
    return value if value is not None else 0


def media_url(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, dict):
        for key in (
            "url", "src", "image_url", "imageUrl", "cover_url", "coverUrl",
            "thumbnail", "thumbnailUrl", "poster", "posterUrl",
            "download_url", "downloadUrl", "play_url", "playUrl", "file_url", "fileUrl",
        ):
            candidate = value.get(key)
            if candidate:
                url = media_url(candidate)
                if url:
                    return url
        for nested_key in ("image", "cover", "thumbnail", "poster", "srcUrl", "origin", "original", "default"):
            candidate = value.get(nested_key)
            if candidate:
                url = media_url(candidate)
                if url:
                    return url
        return ""
    if isinstance(value, list):
        for item in value:
            url = media_url(item)
            if url:
                return url
    return ""


def media_list(value: Any) -> List[str]:
    items: List[str] = []
    if isinstance(value, dict):
        for key in ("image_list", "imageList", "images", "image_urls", "imageUrls", "pics", "pictures", "photoList", "covers", "items", "list"):
            candidate = value.get(key)
            if candidate is not None:
                for url in media_list(candidate):
                    if url and url not in items:
                        items.append(url)
        url = media_url(value)
        if url and url not in items:
            items.append(url)
        return items
    if isinstance(value, list):
        for item in value:
            if isinstance(item, list):
                for url in media_list(item):
                    if url and url not in items:
                        items.append(url)
            else:
                url = media_url(item)
                if url and url not in items:
                    items.append(url)
        return items
    url = media_url(value)
    if url:
        items.append(url)
    return items


def coerce_timestamp_seconds(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return 0
        if number >= 1_000_000_000_000_000:
            number /= 1_000_000
        elif number >= 1_000_000_000_000:
            number /= 1_000
        return int(number)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        if text.isdigit():
            return coerce_timestamp_seconds(int(text))
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return 0


def format_timestamp_parts(value: Any) -> Tuple[str, str, str]:
    ts = coerce_timestamp_seconds(value)
    if ts <= 0:
        ts = int(datetime.now().timestamp())
    dt = datetime.fromtimestamp(ts)
    iso = dt.isoformat(timespec="seconds")
    return iso[:10], iso[11:16], iso


def extract_content_context(url: str, platform: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    path = parsed.path or ""
    host = (parsed.netloc or "").lower()
    platform_key = scraper_platform_key(platform)
    context: Dict[str, Any] = {}
    if platform_key == "xhs":
        match = re.search(r"/(?:explore|discovery/item|notes|item)/([^/?#]+)", path, re.I)
        if match:
            context["content_id"] = match.group(1)
        context["xsec_token"] = (query.get("xsec_token") or [""])[0].strip()
        context["xsec_source"] = (query.get("xsec_source") or [""])[0].strip()
        return context
    if platform_key in {"bilibili", "douyin", "tiktok", "kuaishou", "twitter"}:
        patterns = {
            "bilibili": [r"/video/([^/?#]+)"],
            "douyin": [r"/video/([^/?#]+)"],
            "tiktok": [r"/@[^/]+/video/([^/?#]+)", r"/video/([^/?#]+)"],
            "kuaishou": [r"/short-video/([^/?#]+)"],
            "twitter": [r"/status/([^/?#]+)"],
        }
        for pattern in patterns.get(platform_key, []):
            match = re.search(pattern, path, re.I)
            if match:
                context["content_id"] = match.group(1)
                return context
        if platform_key == "twitter" and "/i/status/" in path:
            match = re.search(r"/i/status/([^/?#]+)", path, re.I)
            if match:
                context["content_id"] = match.group(1)
        return context
    if platform_key == "youtube":
        video_id = (query.get("v") or [""])[0].strip()
        if video_id:
            context["content_id"] = video_id
            return context
        match = re.search(r"/(?:shorts|embed|live)/([^/?#]+)", path, re.I)
        if match:
            context["content_id"] = match.group(1)
            return context
        if host.endswith("youtu.be"):
            match = re.search(r"/([^/?#]+)", path)
            if match:
                context["content_id"] = match.group(1)
        return context
    return context


def stable_public_url(url: str, platform: str = "") -> str:
    """Return a persistent public URL with temporary access parameters removed."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    detected_platform = platform or detect_platform_from_url(raw)
    if scraper_platform_key(detected_platform) == "xhs":
        context = extract_content_context(raw, detected_platform or "小红书")
        content_id = first_text_value(context.get("content_id"))
        if content_id:
            return f"https://www.xiaohongshu.com/explore/{content_id}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"xsec_token", "xsec_source"}
        ],
        doseq=True,
    )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, query, ""))


def stable_account_profile_url(url: str) -> str:
    """Return an account homepage URL without query parameters or fragments."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))


def redact_temporary_access_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(
        r"(?:[?&]\s*)?xsec_(?:token|source)\s*=\s*[^&\s\"'<>]+",
        "",
        text,
        flags=re.I,
    )
    text = text.replace("?&", "?").replace("&&", "&")
    return re.sub(r"[?&](?=\s|$|[\]\[(){}.,;:])", "", text)


def is_interactive_verification_error(value: Any) -> bool:
    text = redact_temporary_access_text(value).casefold()
    return any(marker in text for marker in (
        "rate_limited",
        "interactive verification",
        "verification_wall",
        "安全验证",
        "访问频繁",
        "扫码查看",
        "打开小红书app扫码",
    ))


def sanitize_snapshot_for_storage(snapshot: Dict[str, Any], stable_url: str, platform: str) -> Dict[str, Any]:
    """Remove temporary XHS access context before a snapshot is persisted."""
    forbidden_keys = {"xsectoken", "xsecsource"}

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if re.sub(r"[^a-z0-9]", "", str(key).lower()) not in forbidden_keys
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                value = stable_public_url(value, detect_platform_from_url(value))
            return redact_temporary_access_text(value)
        return value

    cleaned = clean(dict(snapshot or {}))
    cleaned["url"] = stable_url
    context = cleaned.get("content_context")
    if isinstance(context, dict):
        cleaned["content_context"] = {
            key: value for key, value in context.items()
            if re.sub(r"[^a-z0-9]", "", str(key).lower()) not in forbidden_keys
        }
    return cleaned


def scraper_service_request(endpoint: str, payload: Dict[str, Any], request_id: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not SCRAPER_SERVICE_URL:
        raise RuntimeError("scraper service url is not configured")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_id = (request_id or uuid.uuid4().hex).strip()
    headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
    if SCRAPER_SERVICE_API_KEY:
        headers["X-API-Key"] = SCRAPER_SERVICE_API_KEY
    req = Request(f"{SCRAPER_SERVICE_URL}{endpoint}", data=data, headers=headers, method="POST")
    context = ssl._create_unverified_context()
    try:
        with urlopen(req, timeout=SCRAPER_SERVICE_TIMEOUT, context=context) as resp:
            raw = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
            parsed = safe_json_loads(raw, {})
            if not isinstance(parsed, dict):
                raise RuntimeError(f"unexpected scraper response: {type(parsed).__name__}")
            meta = {
                "status_code": resp.status,
                "request_id": resp.headers.get("X-Request-ID", request_id),
                "endpoint": endpoint,
                "url": f"{SCRAPER_SERVICE_URL}{endpoint}",
            }
            return parsed, meta
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        parsed = safe_json_loads(body, {})
        error = parsed.get("error") if isinstance(parsed, dict) else {}
        if not isinstance(error, dict):
            error = {}
        code = error.get("code") or f"HTTP_{exc.code}"
        message = error.get("message") or parsed.get("detail") or body or str(exc)
        raise RuntimeError(f"{code}: {message}") from exc


def scraper_service_get(endpoint: str, request_id: str = "") -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Call a scraper GET endpoint without exposing credentials to the panel UI."""
    if not SCRAPER_SERVICE_URL:
        raise RuntimeError("scraper service url is not configured")
    request_id = (request_id or uuid.uuid4().hex).strip()
    headers = {"Accept": "application/json", "X-Request-ID": request_id}
    if SCRAPER_SERVICE_API_KEY:
        headers["X-API-Key"] = SCRAPER_SERVICE_API_KEY
    req = Request(f"{SCRAPER_SERVICE_URL}{endpoint}", headers=headers, method="GET")
    context = ssl._create_unverified_context()
    try:
        with urlopen(req, timeout=SCRAPER_SERVICE_TIMEOUT, context=context) as resp:
            raw = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
            parsed = safe_json_loads(raw, {})
            if not isinstance(parsed, dict):
                raise RuntimeError(f"unexpected scraper response: {type(parsed).__name__}")
            meta = {
                "status_code": resp.status,
                "request_id": resp.headers.get("X-Request-ID", request_id),
                "endpoint": endpoint,
                "url": f"{SCRAPER_SERVICE_URL}{endpoint}",
            }
            return parsed, meta
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        parsed = safe_json_loads(body, {})
        error = parsed.get("error") if isinstance(parsed, dict) else {}
        if not isinstance(error, dict):
            error = {}
        code = error.get("code") or f"HTTP_{exc.code}"
        message = error.get("message") or parsed.get("detail") or body or str(exc)
        raise RuntimeError(f"{code}: {message}") from exc


def scraper_browser_status(request_id: str = "") -> Dict[str, Any]:
    response, _ = scraper_service_get("/api/browser/status", request_id=request_id)
    return response


def scraper_browser_action(endpoint: str, request_id: str = "") -> Dict[str, Any]:
    response, _ = scraper_service_request(endpoint, {}, request_id=request_id)
    return response


def scraper_service_info(
    platform: str,
    content_id: str,
    request_id: str = "",
    extra: str = "",
    *,
    kwargs: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if fetch_interval_platform_key(platform):
        platform_fetch_pause(platform)
    payload = {
        "platform": platform,
        "content_id": content_id,
        "extra": extra or "",
        "kwargs": kwargs or {},
        "auth_context": None,
        "proxy_context": None,
    }
    response, meta = scraper_service_request("/api/scrape/info", payload, request_id=request_id)
    note_info = response.get("note_info")
    if not isinstance(note_info, dict):
        raise RuntimeError("scraper service returned empty note_info")
    return note_info, meta


def scraper_service_comments(platform: str, content_id: str, request_id: str = "", *, xsec_token: str = "", extra: str = "", xsec_source: str = "", kwargs: Dict[str, Any] | None = None, auth_context: Dict[str, Any] | None = None, proxy_context: Dict[str, Any] | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any] | None, Dict[str, Any]]:
    if fetch_interval_platform_key(platform):
        platform_fetch_pause(platform)
    payload = {
        "platform": platform,
        "content_id": content_id,
        "xsec_token": xsec_token or "",
        "extra": extra or "",
        "xsec_source": xsec_source or "",
        "kwargs": kwargs or {},
        "auth_context": auth_context,
        "proxy_context": proxy_context,
    }
    response, meta = scraper_service_request("/api/scrape/comments", payload, request_id=request_id)
    comments = response.get("comments")
    note_info = response.get("note_info")
    if not isinstance(comments, list):
        comments = []
    if not isinstance(note_info, dict):
        note_info = None
    return comments, note_info, meta


def scraper_service_search(platform: str, keyword: str, limit: int = 20, request_id: str = "", *, kwargs: Dict[str, Any] | None = None, auth_context: Dict[str, Any] | None = None, proxy_context: Dict[str, Any] | None = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if fetch_interval_platform_key(platform):
        platform_fetch_pause(platform)
    payload = {
        "platform": platform,
        "keyword": keyword,
        "limit": limit,
        "kwargs": kwargs or {},
        "auth_context": auth_context,
        "proxy_context": proxy_context,
    }
    response, meta = scraper_service_request("/api/scrape/search", payload, request_id=request_id)
    results = response.get("results")
    if not isinstance(results, list):
        results = []
    return results, meta


def scraper_service_call(platform: str, method: str, request_id: str = "", *, args: List[Any] | None = None, kwargs: Dict[str, Any] | None = None, auth_context: Dict[str, Any] | None = None, proxy_context: Dict[str, Any] | None = None) -> Tuple[Any, Dict[str, Any]]:
    if fetch_interval_platform_key(platform):
        platform_fetch_pause(platform)
    payload = {
        "platform": platform,
        "method": method,
        "args": args or [],
        "kwargs": kwargs or {},
        "auth_context": auth_context,
        "proxy_context": proxy_context,
    }
    response, meta = scraper_service_request("/api/scrape/call", payload, request_id=request_id)
    return response.get("result"), meta


def scraper_service_account_contents(
    platform: str,
    identifier: str,
    account_name: str,
    limit: int = 20,
    request_id: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Discover real posts from one platform account/profile.

    The scraper owns identity resolution. It may use an exact-name fallback,
    but the route is forbidden from degrading this operation into ordinary
    keyword search.
    """
    result, meta = scraper_service_call(
        platform,
        "get_user_videos",
        request_id=request_id,
        args=[identifier],
        kwargs={
            "identifier": identifier,
            "profile_url": identifier if identifier.startswith(("http://", "https://")) else "",
            "account_name": account_name,
            "max_results": max(1, min(int(limit or 20), 50)),
        },
    )
    if not isinstance(result, list):
        raise RuntimeError("scraper service returned invalid account contents")
    return [item for item in result if isinstance(item, dict)], meta


def normalize_service_note_info(platform: str, note_info: Dict[str, Any], content_id: str = "") -> Dict[str, Any]:
    raw = note_info if isinstance(note_info, dict) else {}
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    author = first_text_value(
        raw.get("author"),
        user.get("nickname"),
        user.get("name"),
        owner.get("name"),
        raw.get("nickname"),
        raw.get("author_name"),
        raw.get("uploader"),
        raw.get("channelTitle"),
    )
    content_id_value = first_text_value(
        raw.get("content_id"),
        raw.get("note_id"),
        raw.get("noteId"),
        raw.get("bvid"),
        raw.get("video_id"),
        raw.get("videoId"),
        raw.get("item_id"),
        raw.get("itemId"),
        raw.get("aweme_id"),
        raw.get("awemeId"),
        raw.get("photo_id"),
        raw.get("photoId"),
        content_id,
    )
    title = first_text_value(
        raw.get("title"),
        raw.get("displayTitle"),
        raw.get("desc"),
        raw.get("body"),
        raw.get("caption"),
        raw.get("text"),
        raw.get("content"),
        nested_value(raw, "video", "title"),
    )
    description = first_text_value(
        raw.get("desc"),
        raw.get("body"),
        raw.get("caption"),
        raw.get("summary"),
        raw.get("text"),
        raw.get("content"),
    )
    images = media_list(
        raw.get("image_list") or raw.get("imageList") or raw.get("images") or raw.get("pics") or raw.get("pictures") or raw.get("photoList") or raw.get("photo_list") or raw.get("covers") or []
    )
    cover = first_text_value(
        media_url(raw.get("cover")),
        media_url(raw.get("coverUrl")),
        media_url(raw.get("cover_url")),
        media_url(raw.get("thumbnail")),
        media_url(raw.get("thumbnailUrl")),
        media_url(raw.get("poster")),
        media_url(nested_value(raw, "video", "cover")),
        media_url(nested_value(raw, "video", "coverUrl")),
        media_url(nested_value(raw, "video", "thumbnail")),
        media_url(nested_value(raw, "video", "playUrl")),
        images[0] if images else "",
    )
    if not images and cover:
        images = [cover]
    views = first_optional_count_value(
        nested_value(raw, "metrics", "views"),
        nested_value(raw, "metrics", "view_count"),
        nested_value(raw, "metrics", "play_count"),
        nested_value(raw, "interact_info", "view_count"),
        nested_value(raw, "interactInfo", "viewCount"),
        nested_value(raw, "statistics", "view_count"),
        nested_value(raw, "statistics", "play_count"),
        nested_value(raw, "stats", "playCount"),
        raw.get("view_count"),
        raw.get("play_count"),
        raw.get("play"),
        raw.get("playCount"),
        raw.get("view"),
    )
    likes = first_optional_count_value(
        nested_value(raw, "metrics", "likes"),
        nested_value(raw, "metrics", "liked_count"),
        nested_value(raw, "interact_info", "liked_count"),
        nested_value(raw, "interactInfo", "likedCount"),
        nested_value(raw, "statistics", "digg_count"),
        nested_value(raw, "statistics", "like_count"),
        nested_value(raw, "stats", "diggCount"),
        nested_value(raw, "stats", "likeCount"),
        raw.get("like_count"),
        raw.get("liked_count"),
        raw.get("digg_count"),
        raw.get("diggCount"),
        raw.get("like"),
    )
    saves = first_optional_count_value(
        nested_value(raw, "metrics", "saves"),
        nested_value(raw, "metrics", "collected_count"),
        nested_value(raw, "interact_info", "collected_count"),
        nested_value(raw, "interactInfo", "collectedCount"),
        nested_value(raw, "statistics", "collect_count"),
        nested_value(raw, "stats", "collectCount"),
        raw.get("collect_count"),
        raw.get("collected_count"),
        raw.get("favorite_count"),
        raw.get("bookmark_count"),
        raw.get("favorite"),
        raw.get("collect"),
    )
    comments = first_optional_count_value(
        nested_value(raw, "metrics", "comments"),
        nested_value(raw, "interact_info", "comment_count"),
        nested_value(raw, "interactInfo", "commentCount"),
        nested_value(raw, "statistics", "comment_count"),
        nested_value(raw, "stats", "commentCount"),
        raw.get("comment_count"),
        raw.get("commentCount"),
        raw.get("reply_count"),
        raw.get("replyCount"),
        raw.get("reply"),
    )
    shares = first_optional_count_value(
        nested_value(raw, "metrics", "shares"),
        nested_value(raw, "metrics", "share_count"),
        nested_value(raw, "interact_info", "shared_count"),
        nested_value(raw, "interactInfo", "sharedCount"),
        nested_value(raw, "statistics", "share_count"),
        nested_value(raw, "stats", "shareCount"),
        raw.get("share_count"),
        raw.get("shared_count"),
        raw.get("shareCount"),
        raw.get("reposts"),
        raw.get("repost_count"),
    )
    extra_metrics: Dict[str, Any] = {}
    if shares is not None:
        extra_metrics["shares"] = shares
    coins = first_count_value(raw.get("coin_count"), raw.get("coins"), nested_value(raw, "statistics", "coin_count"))
    if coins:
        extra_metrics["coins"] = coins
    danmaku = first_count_value(raw.get("danmaku_count"), raw.get("danmaku"), nested_value(raw, "statistics", "danmaku_count"))
    if danmaku:
        extra_metrics["danmaku"] = danmaku
    publish_candidates = (
        raw.get("publish_time"),
        raw.get("published_at"),
        raw.get("pubdate"),
        raw.get("timestamp"),
        raw.get("created_at"),
        raw.get("createdAt"),
        raw.get("createdTime"),
        raw.get("create_time"),
        raw.get("createTime"),
        raw.get("time"),
        raw.get("upload_date"),
        nested_value(raw, "snippet", "publishedAt"),
    )
    publish_raw = next((value for value in publish_candidates if value not in (None, "")), None)
    publish_ts = coerce_timestamp_seconds(publish_raw)
    if not publish_ts and platform == "xhs" and len(content_id_value) >= 8:
        try:
            publish_ts = int(content_id_value[:8], 16)
        except ValueError:
            publish_ts = 0
    published_at = datetime.fromtimestamp(publish_ts).isoformat(timespec="seconds") if publish_ts else ""
    metric_presence = raw.get("metric_presence") if isinstance(raw.get("metric_presence"), dict) else {}
    media_type = first_text_value(raw.get("media_type"), raw.get("mediaType"), raw.get("content_type"), raw.get("contentType"))
    if media_type.lower() in {"video", "short_video", "video_note"}:
        media_type = "video"
        images = []
    elif images:
        media_type = "image"
    return {
        "content_id": content_id_value,
        "title": title,
        "description": description,
        "author": author,
        "media_type": media_type,
        "cover": cover,
        "images": images,
        "metrics": {"views": views, "likes": likes, "saves": saves, "comments": comments, "shares": shares},
        "metric_presence": metric_presence,
        "extra_metrics": extra_metrics,
        "publish_time": publish_ts,
        "published_at": published_at,
        "url": first_text_value(raw.get("url"), raw.get("link"), raw.get("webpage_url"), raw.get("webpageUrl"), raw.get("share_url"), raw.get("shareUrl")),
    }


def build_service_snapshot(url: str, platform: str, note_info: Dict[str, Any], meta: Dict[str, Any], connector_info: Dict[str, Any], content_context: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_service_note_info(platform, note_info, content_context.get("content_id", ""))
    title = normalized.get("title") or normalized.get("description") or ""
    author = normalized.get("author") or ""
    cover = normalized.get("cover") or ""
    images = normalized.get("images") or []
    if not cover and images:
        cover = images[0]
    if cover and not images:
        images = [cover]
    publish_ts = int(normalized.get("publish_time") or 0)
    published_at = normalized.get("published_at") or ""
    if not published_at and publish_ts:
        published_at = datetime.fromtimestamp(publish_ts).isoformat(timespec="seconds")
    published_date = published_at[:10] if published_at else ""
    published_time = published_at[11:16] if published_at else ""
    snapshot = {
        "url": url,
        "platform": platform,
        "connector": connector_info,
        "status": "fetched",
        "content_id": normalized.get("content_id") or content_context.get("content_id") or "",
        "title": title,
        "description": normalized.get("description") or "",
        "site_name": connector_info.get("name") or platform,
        "author": author,
        "media_type": normalized.get("media_type") or "",
        "cover": cover,
        "images": images,
        "metrics": normalized.get("metrics") or {"views": None, "likes": None, "saves": None, "comments": None},
        "metric_presence": normalized.get("metric_presence") or {},
        "collection_mode": connector_info["sync_scope"],
        "extra_metrics": normalized.get("extra_metrics") or {},
        "error": "",
        "captured_at": now_iso(),
        "published_at": published_at,
        "published_date": published_date,
        "published_time": published_time,
        "scraper_meta": meta,
        "content_context": content_context,
    }
    if not snapshot["cover"] and snapshot["images"]:
        snapshot["cover"] = snapshot["images"][0]
    if snapshot["cover"] and not snapshot["images"] and snapshot.get("media_type") != "video":
        snapshot["images"] = [snapshot["cover"]]
    return mark_snapshot_if_incomplete(snapshot)


def discovery_content_id(candidate: Dict[str, Any]) -> str:
    return first_text_value(
        candidate.get("content_id"),
        candidate.get("note_id"),
        candidate.get("noteId"),
        candidate.get("aweme_id"),
        candidate.get("awemeId"),
        candidate.get("photo_id"),
        candidate.get("photoId"),
        candidate.get("video_id"),
        candidate.get("videoId"),
    )


def normalized_account_identity(value: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(str(value or "")).lstrip("@")).casefold()


def discovery_detail_identifier(platform_key: str, candidate: Dict[str, Any], content_id: str) -> str:
    if platform_key == "kuaishou":
        return re.sub(r"_ccc$", "", content_id, flags=re.I)
    if platform_key != "xhs":
        return content_id
    url_context = extract_content_context(first_text_value(candidate.get("url"), candidate.get("link")), "小红书")
    token = first_text_value(candidate.get("xsec_token"), candidate.get("xsecToken"), url_context.get("xsec_token"))
    source = first_text_value(candidate.get("xsec_source"), candidate.get("xsecSource"), url_context.get("xsec_source"), "pc_note")
    if not token:
        return content_id
    return f"{content_id}?xsec_token={token}&xsec_source={source}"


def discovery_content_url(platform_key: str, candidate: Dict[str, Any], content_id: str) -> str:
    url = first_text_value(candidate.get("url"), candidate.get("link"), candidate.get("share_url"))
    if url:
        return url
    if platform_key == "douyin":
        return f"https://www.douyin.com/video/{content_id}"
    if platform_key == "kuaishou":
        return f"https://www.kuaishou.com/short-video/{re.sub(r'_ccc$', '', content_id, flags=re.I)}"
    if platform_key == "xhs":
        detail_id = discovery_detail_identifier(platform_key, candidate, content_id)
        if "?" in detail_id:
            note_id, query = detail_id.split("?", 1)
            return f"https://www.xiaohongshu.com/explore/{note_id}?{query}"
        return f"https://www.xiaohongshu.com/explore/{content_id}"
    return ""


def prepare_account_discovered_posts(
    account: sqlite3.Row,
    discovered: List[Dict[str, Any]],
    discovery_meta: Dict[str, Any],
    request_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    platform = account["platform"]
    platform_key = scraper_platform_key(platform)
    prepared: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for candidate_index, candidate in enumerate(discovered):
        content_id = discovery_content_id(candidate)
        if not content_id:
            blocked.append({
                "url": stable_public_url(first_text_value(candidate.get("url")), platform),
                "error": "账号作品缺少平台原生内容 ID",
            })
            continue
        detail_identifier = discovery_detail_identifier(platform_key, candidate, content_id)
        url = discovery_content_url(platform_key, candidate, content_id)
        if platform_key == "xhs":
            detail_identifier = content_id
            url = stable_public_url(url, platform)
        dedupe_key = f"{platform_key}:{detail_identifier}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        connector = resolve_connector(url, platform)
        connector_info = {
            "key": connector.get("key", account["connector_key"] or "generic"),
            "name": connector.get("name", "通用网页连接器"),
            "platform": platform,
            "matched_by": connector.get("matched_by", "account_discovery"),
            "capabilities": connector.get("capabilities", ["account_sync", "link_capture"]),
            "domains": connector.get("domains", []),
            "sync_scope": connector.get("sync_scope", connector_sync_policy(platform)["sync_scope"]),
            "sync_label": connector.get("sync_label", connector_sync_policy(platform)["sync_label"]),
            "sync_note": connector.get("sync_note", connector_sync_policy(platform)["sync_note"]),
        }
        content_context = extract_content_context(url, platform) if url else {}
        content_context["content_id"] = re.sub(r"_ccc$", "", content_id, flags=re.I) if platform_key == "kuaishou" else content_id
        try:
            note_info, detail_meta = scraper_service_info(
                platform_key,
                detail_identifier,
                request_id=request_id,
                extra=url,
                kwargs={"account_import": True} if platform_key == "xhs" else None,
            )
            snapshot = build_service_snapshot(url, platform, note_info, detail_meta, connector_info, content_context)
            discovery_strength = candidate.get("discovery_strength") or "unknown"
            snapshot["account_discovery"] = {
                "identifier_strength": discovery_strength,
                "profile_url": stable_account_profile_url(
                    candidate.get("profile_url") or account["profile_url"] or ""
                ),
                "candidate_author": candidate.get("author") or "",
                "service_meta": discovery_meta,
            }
            if str(discovery_strength).startswith("weak") and normalized_account_identity(snapshot.get("author")) != normalized_account_identity(account["account_name"]):
                blocked.append({
                    "url": stable_public_url(url, platform),
                    "error": f"弱账号名发现的详情作者不匹配：{snapshot.get('author') or '未抓到作者'}",
                })
                continue
            if snapshot.get("status") != "fetched":
                blocked.append({
                    "url": stable_public_url(url, platform),
                    "error": snapshot.get("error") or "详情强制字段缺失",
                    "missing_fields": snapshot.get("missing_fields") or [],
                })
                continue
            prepared.append({
                "published_url": url,
                "platform": platform,
                "account": account["account_name"],
                "title": snapshot.get("title") or "",
                "cover": snapshot.get("cover") or "",
                "images": snapshot.get("images") or [],
                "metrics": snapshot.get("metrics") or {},
                "published_date": snapshot.get("published_date") or snapshot.get("captured_at", now_iso())[:10],
                "published_time": snapshot.get("published_time") or snapshot.get("captured_at", now_iso())[11:16],
                "note": snapshot.get("description") or "",
                "published_snapshot_json": snapshot,
                "source": "account_discovery",
            })
        except Exception as exc:
            message = redact_temporary_access_text(exc)
            blocked_item: Dict[str, Any] = {
                "url": stable_public_url(url, platform),
                "error": f"真实详情抓取失败：{message}",
            }
            if platform_key == "xhs" and is_interactive_verification_error(message):
                blocked_item.update({
                    "paused": True,
                    "resumable": True,
                    "remaining_count": max(1, len(discovered) - candidate_index),
                })
                blocked.append(blocked_item)
                break
            blocked.append(blocked_item)
    return prepared, blocked



def resolve_supported_detail_url(url: str, timeout: int = 15) -> str:
    """Follow a supported platform share-link redirect without parsing page data."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
    }
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, headers=headers, method=method)
            with urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as response:
                return response.geturl() or url
        except HTTPError as exc:
            if 300 <= exc.code < 400 and exc.headers.get("Location"):
                return urljoin(url, exc.headers["Location"])
            if method == "HEAD" and exc.code in {400, 403, 405}:
                continue
            raise
    return url


def capture_link_snapshot(
    url: str,
    platform_hint: str = "",
    request_id: str = "",
    *,
    account_import: bool = False,
) -> Dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("link must start with http:// or https://")
    connector = resolve_connector(url, platform_hint)
    validate_formal_capture_url(url, connector)
    platform = connector.get("platform") or platform_hint or detect_platform_from_url(url)
    connector_info = {
        "key": connector.get("key", "generic"),
        "name": connector.get("name", "通用网页连接器"),
        "platform": platform,
        "matched_by": connector.get("matched_by", "generic"),
        "capabilities": connector.get("capabilities", ["link_capture"]),
        "domains": connector.get("domains", []),
        "sync_scope": connector.get("sync_scope", connector_sync_policy(platform)["sync_scope"]),
        "sync_label": connector.get("sync_label", connector_sync_policy(platform)["sync_label"]),
        "sync_note": connector.get("sync_note", connector_sync_policy(platform)["sync_note"]),
    }
    if platform in PUBLIC_ONLY_SET and not is_public_wechat_url(url):
        return {
            "url": url,
            "platform": platform,
            "connector": connector_info,
            "status": "blocked",
            "title": "",
            "description": "",
            "site_name": "",
            "author": "",
            "cover": "",
            "images": [],
            "metrics": {"views": None, "likes": None, "saves": None, "comments": None, "shares": None},
            "collection_mode": connector_info["sync_scope"],
            "error": "微信公众号后台链接不参与同步，只允许公开文章页 / 公开页抓取",
            "captured_at": now_iso(),
        }

    content_context = extract_content_context(url, platform)
    scraper_platform = scraper_platform_key(platform)
    if scraper_platform and not content_context.get("content_id"):
        try:
            resolved_url = resolve_supported_detail_url(url)
        except Exception as exc:
            raise ValueError(f"平台分享链接解析失败：{exc}") from exc
        if resolved_url != url:
            resolved_connector = resolve_connector(resolved_url, platform_hint)
            validate_formal_capture_url(resolved_url, resolved_connector)
            resolved_platform = resolved_connector.get("platform") or platform
            if resolved_platform != platform:
                raise ValueError("平台分享链接跳转到了不匹配的站点")
            url = resolved_url
            connector = resolved_connector
            content_context = extract_content_context(url, platform)
    if FORMAL_MODE and platform in STRICT_CAPTURE_PLATFORMS and not content_context.get("content_id"):
        raise ValueError("无法从平台链接识别真实作品编号，已禁止普通网页降级")
    if scraper_platform == "kuaishou" and content_context.get("content_id"):
        content_context["content_id"] = re.sub(r"_ccc$", "", content_context["content_id"], flags=re.I)
    if scraper_platform and content_context.get("content_id"):
        try:
            service_content_id = content_context["content_id"]
            if scraper_platform == "xhs" and content_context.get("xsec_token"):
                source = content_context.get("xsec_source") or "pc_note"
                service_content_id = (
                    f"{service_content_id}?xsec_token={content_context['xsec_token']}"
                    f"&xsec_source={source}"
                )
            note_info, meta = scraper_service_info(
                scraper_platform,
                service_content_id,
                request_id=request_id,
                extra=url,
                kwargs={"account_import": True} if account_import and scraper_platform == "xhs" else None,
            )
            snapshot = build_service_snapshot(url, platform, note_info, meta, connector_info, content_context)
            snapshot["fetch_policy"] = {
                "mode": "scraper_service",
                "service_url": SCRAPER_SERVICE_URL,
                "timeout_seconds": SCRAPER_SERVICE_TIMEOUT,
            }
            return snapshot
        except Exception as exc:
            logger.warning(
                "scraper service capture failed for %s (%s): %s",
                stable_public_url(url, platform),
                scraper_platform,
                redact_temporary_access_text(exc),
            )
            if platform in STRICT_CAPTURE_PLATFORMS:
                return {
                    "url": url,
                    "platform": platform,
                    "connector": connector_info,
                    "status": "error",
                    "content_id": content_context.get("content_id") or "",
                    "title": "",
                    "description": "",
                    "site_name": connector_info.get("name") or platform,
                    "author": "",
                    "cover": "",
                    "images": [],
                    "metrics": {"views": None, "likes": None, "saves": None, "comments": None, "shares": None},
                    "collection_mode": connector_info["sync_scope"],
                    "extra_metrics": {},
                    "missing_fields": sorted(REQUIRED_CAPTURE_FIELDS),
                    "error": f"真实抓取失败，已阻止普通网页降级：{redact_temporary_access_text(exc)}",
                    "captured_at": now_iso(),
                    "scraper_meta": {"backend": f"{scraper_platform}-real", "degraded": False},
                    "content_context": content_context,
                }

    try:
        if platform == "小红书":
            last_exc = None
            for attempt in range(XIAOHONGSHU_FETCH_POLICY["max_retries"] + 1):
                try:
                    xiaohongshu_fetch_pause(attempt=attempt)
                    html_text = fetch_url_text(url, timeout=XIAOHONGSHU_FETCH_POLICY["timeout_seconds"])
                    meta = extract_metadata(html_text, url)
                    snapshot = build_platform_snapshot(url, platform, html_text, meta, connector_info)
                    snapshot["fetch_policy"] = {
                        "min_interval_seconds": XIAOHONGSHU_FETCH_POLICY["min_interval_seconds"],
                        "max_interval_seconds": XIAOHONGSHU_FETCH_POLICY["max_interval_seconds"],
                        "timeout_seconds": XIAOHONGSHU_FETCH_POLICY["timeout_seconds"],
                        "max_retries": XIAOHONGSHU_FETCH_POLICY["max_retries"],
                    }
                    return snapshot
                except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                    last_exc = exc
                    if attempt >= XIAOHONGSHU_FETCH_POLICY["max_retries"]:
                        raise
            raise last_exc if last_exc else RuntimeError("xiaohongshu fetch failed")
        fallback_platform = fetch_interval_platform_key(platform)
        if fallback_platform != "xhs":
            platform_fetch_pause(fallback_platform)
        html_text = fetch_url_text(url)
        meta = extract_metadata(html_text, url)
        snapshot = build_platform_snapshot(url, platform, html_text, meta, connector_info)
        return snapshot
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        return {
            "url": url,
            "platform": platform,
            "connector": connector_info,
            "status": "error",
            "title": "",
            "description": "",
            "site_name": "",
            "author": "",
            "cover": "",
            "images": [],
            "metrics": {"views": None, "likes": None, "saves": None, "comments": None, "shares": None},
            "collection_mode": connector_info["sync_scope"],
            "error": str(exc),
            "captured_at": now_iso(),
        }


def optional_metric_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def attach_real_comments(
    snapshot: Dict[str, Any],
    url: str,
    request_id: str = "",
    *,
    account_import: bool = False,
) -> Dict[str, Any]:
    """Attach real public comments without downgrading to generated or mock data."""
    snapshot = dict(snapshot or {})
    snapshot["comments_data"] = []
    snapshot["comments_error"] = ""
    if snapshot.get("status") != "fetched":
        return snapshot
    platform = snapshot.get("platform") or detect_platform_from_url(url)
    scraper_platform = scraper_platform_key(platform)
    context = snapshot.get("content_context") if isinstance(snapshot.get("content_context"), dict) else extract_content_context(url, platform)
    content_id = first_text_value(snapshot.get("content_id"), context.get("content_id"))
    if scraper_platform == "kuaishou":
        content_id = re.sub(r"_ccc$", "", content_id, flags=re.I)
    if not scraper_platform or not content_id:
        snapshot["comments_error"] = "当前链接尚无法识别评论接口所需的作品编号"
        return snapshot
    try:
        comments, _, meta = scraper_service_comments(
            scraper_platform,
            content_id,
            request_id=request_id,
            xsec_token=context.get("xsec_token") or "",
            xsec_source=context.get("xsec_source") or "",
            extra=url,
            kwargs={
                "reuse_current_page": True,
                "include_info": False,
                **({"account_import": True} if account_import and scraper_platform == "xhs" else {}),
            },
        )
        backend = str((meta or {}).get("backend") or "")
        if (meta or {}).get("degraded") or "mock" in backend.lower():
            raise RuntimeError("评论接口未返回真实抓取结果")
        snapshot["comments_data"] = [item for item in comments[:20] if isinstance(item, dict)]
        snapshot["comments_captured_at"] = now_iso()
        snapshot["comments_meta"] = meta or {}
    except Exception as exc:
        message = redact_temporary_access_text(exc)
        if account_import and scraper_platform == "xhs" and is_interactive_verification_error(message):
            raise RuntimeError(message) from exc
        snapshot["comments_error"] = message
        snapshot["comments_captured_at"] = now_iso()
    return snapshot


def record_metric_snapshot(
    conn: sqlite3.Connection,
    archive_id: str,
    platform: str,
    metrics: Dict[str, Any],
    captured_at: str,
    source: str,
) -> None:
    values = {
        key: optional_metric_int((metrics or {}).get(key))
        for key in ("views", "likes", "saves", "comments", "shares")
    }
    if not any(value is not None for value in values.values()):
        return
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO metric_history(
            id, archive_id, platform, views, likes, saves, comments, shares, captured_at, source, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"mh_{uuid.uuid4().hex[:12]}",
            archive_id,
            platform,
            values["views"],
            values["likes"],
            values["saves"],
            values["comments"],
            values["shares"],
            captured_at or ts,
            source,
            ts,
        ),
    )


def save_note_snapshot(
    conn: sqlite3.Connection,
    url: str,
    snapshot: Dict[str, Any],
    *,
    favorite: Optional[bool] = None,
    source: str = "link_capture",
) -> str:
    archive_id, _created = save_note_snapshot_with_status(
        conn,
        url,
        snapshot,
        favorite=favorite,
        source=source,
    )
    return archive_id


def save_note_snapshot_with_status(
    conn: sqlite3.Connection,
    url: str,
    snapshot: Dict[str, Any],
    *,
    favorite: Optional[bool] = None,
    source: str = "link_capture",
) -> Tuple[str, bool]:
    if snapshot.get("status") != "fetched":
        raise ValueError(snapshot.get("error") or "真实笔记抓取未成功")
    title = first_text_value(snapshot.get("title"), snapshot.get("description"))
    if not title:
        raise ValueError("真实抓取结果缺少笔记名称，未写入数据库")
    platform = first_text_value(snapshot.get("platform"), detect_platform_from_url(url))
    url = stable_public_url(url, platform)
    existing = conn.execute("SELECT * FROM archives WHERE published_url = ? LIMIT 1", (url,)).fetchone()
    if existing and not isinstance(snapshot.get("import_context"), dict):
        existing_snapshot = safe_json_loads(existing["published_snapshot_json"] or "{}", {})
        if isinstance(existing_snapshot, dict) and isinstance(existing_snapshot.get("import_context"), dict):
            snapshot["import_context"] = existing_snapshot["import_context"]
    snapshot = sanitize_snapshot_for_storage(snapshot, url, platform)
    published_date = first_text_value(snapshot.get("published_date"), "日期待识别")
    published_time = first_text_value(snapshot.get("published_time"))
    author = first_text_value(snapshot.get("author"), snapshot.get("site_name"))
    description = first_text_value(snapshot.get("description"))
    metrics = dict(snapshot.get("metrics")) if isinstance(snapshot.get("metrics"), dict) else {}
    extra_metrics = snapshot.get("extra_metrics") if isinstance(snapshot.get("extra_metrics"), dict) else {}
    if metrics.get("shares") is None and extra_metrics.get("shares") is not None:
        metrics["shares"] = extra_metrics.get("shares")
    images = [item for item in (snapshot.get("images") or []) if str(item).strip()]
    cover = first_text_value(snapshot.get("cover"), images[0] if images else "")
    ts = now_iso()
    if existing:
        archive_id = existing["id"]
        next_favorite = int(existing["favorite"])
        if favorite is True:
            next_favorite = 1
        conn.execute(
            """
            UPDATE archives
            SET topic = ?, archive_date = ?, archive_time = ?, favorite = ?, note = ?,
                published_url = ?, published_snapshot_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                published_date,
                published_time,
                next_favorite,
                description,
                url,
                json.dumps(snapshot, ensure_ascii=False),
                ts,
                archive_id,
            ),
        )
    else:
        archive_id = create_archive(
            conn,
            {
                "topic": title,
                "archive_date": published_date,
                "archive_time": published_time,
                "favorite": bool(favorite),
                "note": description,
                "published_url": url,
                "published_snapshot_json": snapshot,
            },
            commit=False,
        )
    update_assets(conn, archive_id, cover, images, commit=False)
    if platform in PLATFORMS:
        feedback_metrics = dict(metrics)
        feedback_metrics["account"] = author
        upsert_feedback(conn, archive_id, platform, feedback_metrics, source=source, commit=False)
        if existing is None or source == "refresh":
            record_metric_snapshot(
                conn,
                archive_id,
                platform,
                metrics,
                first_text_value(snapshot.get("captured_at"), ts),
                source,
            )
    return archive_id, existing is None


def init_db() -> None:

    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS archives (
                id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                archive_date TEXT NOT NULL,
                archive_time TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0,
                note TEXT DEFAULT '',
                published_url TEXT NOT NULL DEFAULT '',
                published_snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY,
                archive_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                asset_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                archive_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT '',
                views INTEGER,
                likes INTEGER,
                saves INTEGER,
                comments INTEGER,
                shares INTEGER,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (archive_id, platform),
                FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS metric_history (
                id TEXT PRIMARY KEY,
                archive_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                views INTEGER,
                likes INTEGER,
                saves INTEGER,
                comments INTEGER,
                shares INTEGER,
                captured_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'link_capture',
                created_at TEXT NOT NULL,
                FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_metric_history_archive_time
            ON metric_history(archive_id, captured_at DESC);

            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                account_name TEXT NOT NULL,
                connector_key TEXT NOT NULL DEFAULT '',
                profile_url TEXT NOT NULL DEFAULT '',
                note TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sync_at TEXT NOT NULL DEFAULT '',
                last_sync_status TEXT NOT NULL DEFAULT 'idle',
                last_sync_error TEXT NOT NULL DEFAULT '',
                last_sync_cursor TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (platform, account_name)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                created_count INTEGER NOT NULL DEFAULT 0,
                updated_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS platform_login_state (
                platform TEXT PRIMARY KEY,
                confirmed INTEGER NOT NULL DEFAULT 0,
                confirmed_at TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS content_topics (
                id TEXT PRIMARY KEY,
                archive_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                normalized_topic TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'ai',
                confidence REAL NOT NULL DEFAULT 0,
                confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (archive_id, normalized_topic),
                FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_content_topics_confirmed
            ON content_topics(normalized_topic, confirmed);

            CREATE TABLE IF NOT EXISTS ai_jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                input_fingerprint TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                queued_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_ai_jobs_queue
            ON ai_jobs(status, queued_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_jobs_active_fingerprint
            ON ai_jobs(kind, scope_type, scope_id, input_fingerprint, prompt_version)
            WHERE status IN ('queued', 'running');

            CREATE TABLE IF NOT EXISTS ai_insights (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                input_snapshot_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                generated_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES ai_jobs(id) ON DELETE CASCADE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_insights_fingerprint
            ON ai_insights(kind, scope_type, scope_id, input_fingerprint, prompt_version);
            CREATE INDEX IF NOT EXISTS idx_ai_insights_scope
            ON ai_insights(scope_type, scope_id, generated_at DESC);
            """
        )
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(archives)").fetchall()}
        if "published_url" not in cols:
            conn.execute("ALTER TABLE archives ADD COLUMN published_url TEXT NOT NULL DEFAULT ''")
        if "published_snapshot_json" not in cols:
            conn.execute("ALTER TABLE archives ADD COLUMN published_snapshot_json TEXT NOT NULL DEFAULT '{}' ")
        feedback_info = conn.execute("PRAGMA table_info(feedback)").fetchall()
        cols = {row["name"] for row in feedback_info}
        if "account" not in cols:
            conn.execute("ALTER TABLE feedback ADD COLUMN account TEXT NOT NULL DEFAULT ''")
            feedback_info = conn.execute("PRAGMA table_info(feedback)").fetchall()
            cols = {row["name"] for row in feedback_info}
        if "shares" not in cols:
            conn.execute("ALTER TABLE feedback ADD COLUMN shares INTEGER")
        metric_info = conn.execute("PRAGMA table_info(metric_history)").fetchall()
        metric_cols = {row["name"] for row in metric_info}
        if "shares" not in metric_cols:
            conn.execute("ALTER TABLE metric_history ADD COLUMN shares INTEGER")
        metric_columns = {"views", "likes", "saves", "comments", "shares"}
        if any(row["name"] in metric_columns and int(row["notnull"]) for row in feedback_info):
            conn.executescript(
                """
                ALTER TABLE feedback RENAME TO feedback_legacy_notnull;
                CREATE TABLE feedback (
                    id TEXT PRIMARY KEY,
                    archive_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    views INTEGER,
                    likes INTEGER,
                    saves INTEGER,
                    comments INTEGER,
                    shares INTEGER,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (archive_id, platform),
                    FOREIGN KEY (archive_id) REFERENCES archives(id) ON DELETE CASCADE
                );
                INSERT INTO feedback(
                    id, archive_id, platform, account, views, likes, saves, comments, shares, source, created_at, updated_at
                )
                SELECT id, archive_id, platform, account, views, likes, saves, comments, shares, source, created_at, updated_at
                FROM feedback_legacy_notnull;
                DROP TABLE feedback_legacy_notnull;
                """
            )
        for platform in LOGIN_PLATFORM_DEFINITIONS:
            conn.execute(
                """
                INSERT OR IGNORE INTO platform_login_state(
                    platform, confirmed, confirmed_at, last_checked_at, last_status, last_error
                ) VALUES (?, 0, '', '', 'unknown', '')
                """,
                (platform["key"],),
            )
        conn.commit()


# ----------------------------
# Helpers
# ----------------------------
def login_platform_definition(platform: str) -> Dict[str, str]:
    for item in LOGIN_PLATFORM_DEFINITIONS:
        if item["key"] == platform:
            return item
    raise ValueError("不支持的平台登录项")


def update_login_state(
    conn: sqlite3.Connection,
    platform: str,
    *,
    logged_in: Optional[bool] = None,
    confirmed: Optional[bool] = None,
    error: str = "",
) -> None:
    login_platform_definition(platform)
    row = conn.execute(
        "SELECT confirmed, confirmed_at, last_status FROM platform_login_state WHERE platform = ?",
        (platform,),
    ).fetchone()
    ts = now_iso()
    current_confirmed = bool(row["confirmed"]) if row else False
    current_confirmed_at = str(row["confirmed_at"] or "") if row else ""
    current_status = str(row["last_status"] or "unknown") if row else "unknown"
    next_confirmed = current_confirmed if confirmed is None else bool(confirmed)
    next_confirmed_at = ts if confirmed is True else ("" if confirmed is False else current_confirmed_at)
    next_status = current_status if logged_in is None else ("logged_in" if logged_in else "logged_out")
    conn.execute(
        """
        INSERT INTO platform_login_state(platform, confirmed, confirmed_at, last_checked_at, last_status, last_error)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform) DO UPDATE SET
            confirmed = excluded.confirmed,
            confirmed_at = excluded.confirmed_at,
            last_checked_at = excluded.last_checked_at,
            last_status = excluded.last_status,
            last_error = excluded.last_error
        """,
        (platform, int(next_confirmed), next_confirmed_at, ts, next_status, str(error or "")[:500]),
    )


def login_status_payload(conn: sqlite3.Connection, *, live_checked: bool, error: str = "") -> Dict[str, Any]:
    rows = {
        row["platform"]: row
        for row in conn.execute("SELECT * FROM platform_login_state").fetchall()
    }
    platforms: List[Dict[str, Any]] = []
    for definition in LOGIN_PLATFORM_DEFINITIONS:
        row = rows.get(definition["key"])
        confirmed = bool(row["confirmed"]) if row else False
        logged_in = bool(row and row["last_status"] == "logged_in")
        ready = bool(live_checked and confirmed and logged_in)
        platforms.append({
            "key": definition["key"],
            "name": definition["name"],
            "confirmed": confirmed,
            "confirmed_at": str(row["confirmed_at"] or "") if row else "",
            "last_checked_at": str(row["last_checked_at"] or "") if row else "",
            "logged_in": logged_in,
            "ready": ready,
        })
    return {
        "ok": live_checked,
        "platforms": platforms,
        "confirmed_count": sum(1 for item in platforms if item["confirmed"]),
        "ready_count": sum(1 for item in platforms if item["ready"]),
        "all_confirmed": all(item["confirmed"] for item in platforms),
        "all_ready": all(item["ready"] for item in platforms),
        "signal": "ALL_PLATFORM_LOGINS_CONFIRMED" if all(item["ready"] for item in platforms) else "LOGIN_CONFIRMATION_PENDING",
        "error": str(error or ""),
    }


def refresh_login_states(conn: sqlite3.Connection, request_id: str = "") -> Dict[str, Any]:
    try:
        response = scraper_browser_status(request_id=request_id)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "浏览器登录状态检测失败"))
        logged_in = response.get("logged_in") if isinstance(response.get("logged_in"), dict) else {}
        for definition in LOGIN_PLATFORM_DEFINITIONS:
            update_login_state(
                conn,
                definition["key"],
                logged_in=bool(logged_in.get(definition["key"])),
            )
        conn.commit()
        return login_status_payload(conn, live_checked=True)
    except Exception as exc:
        conn.rollback()
        message = redact_temporary_access_text(exc)
        return login_status_payload(conn, live_checked=False, error=message)


def archive_summary(row: sqlite3.Row, conn: sqlite3.Connection) -> Dict[str, Any]:
    archive_id = row["id"]
    feedback_rows = conn.execute(
        "SELECT platform, account, views, likes, saves, comments, shares, source FROM feedback WHERE archive_id = ? ORDER BY platform",
        (archive_id,),
    ).fetchall()
    assets_rows = conn.execute(
        "SELECT kind, asset_index, title, deleted FROM assets WHERE archive_id = ? ORDER BY kind, asset_index",
        (archive_id,),
    ).fetchall()
    history_rows = conn.execute(
        """
        SELECT platform, views, likes, saves, comments, shares, captured_at, source
        FROM metric_history
        WHERE archive_id = ?
        ORDER BY captured_at DESC, created_at DESC
        LIMIT 30
        """,
        (archive_id,),
    ).fetchall()
    feedback = {
        r["platform"]: {
            "account": r["account"] or "",
            "views": r["views"],
            "likes": r["likes"],
            "saves": r["saves"],
            "comments": r["comments"],
            "shares": r["shares"],
            "source": r["source"],
        }
        for r in feedback_rows
    }
    cover = next((r["title"] for r in assets_rows if r["kind"] == "cover" and not r["deleted"]), "")
    images = [r["title"] for r in assets_rows if r["kind"] == "image" and not r["deleted"]]
    published = safe_json_loads(row["published_snapshot_json"] or "{}", {})
    if not isinstance(published, dict):
        published = {}
    published_url = (row["published_url"] if "published_url" in row.keys() else "") or first_text_value(
        published.get("url"),
        published.get("share_url"),
        published.get("shareUrl"),
        published.get("webpage_url"),
        published.get("webpageUrl"),
    )
    published_title = first_text_value(published.get("title"), published.get("displayTitle"))
    published_description = first_text_value(published.get("description"), published.get("content"))
    original_text = first_text_value(
        published_description,
        published_title,
        row["note"] or "",
        row["topic"] or "",
    )
    import_context = published.get("import_context") if isinstance(published.get("import_context"), dict) else {}
    history_sources = {str(item["source"] or "") for item in history_rows}
    feedback_sources = {str(item["source"] or "") for item in feedback_rows}
    import_source = str(import_context.get("source") or "")
    if import_source != "account_import" and ("account_import" in feedback_sources or "account_import" in history_sources):
        import_source = "account_import"
    account_name = first_text_value(
        import_context.get("account_name"),
        *(fb.get("account") for fb in feedback.values()),
        published.get("author"),
    )
    account_profile_url = stable_account_profile_url(first_text_value(
        import_context.get("account_profile_url"),
        published.get("account_profile_url"),
    ))
    return {
        "id": archive_id,
        "topic": row["topic"],
        "date": row["archive_date"],
        "time": row["archive_time"],
        "favorite": bool(row["favorite"]),
        "note": row["note"] or "",
        "cover": cover,
        "images": images,
        "feedback": feedback,
        "import_source": import_source,
        "account_name": account_name,
        "account_profile_url": account_profile_url,
        "published_url": row["published_url"] if "published_url" in row.keys() else "",
        "original_url": published_url,
        "original_text": original_text,
        "published_title": published_title,
        "published_description": published_description,
        "published": published,
        "metric_history": [
            {
                "platform": item["platform"],
                "views": item["views"],
                "likes": item["likes"],
                "saves": item["saves"],
                "comments": item["comments"],
                "shares": item["shares"],
                "captured_at": item["captured_at"],
                "source": item["source"],
            }
            for item in history_rows
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def note_analysis_payload(row: sqlite3.Row, conn: sqlite3.Connection) -> Dict[str, Any]:
    summary = archive_summary(row, conn)
    published = summary.get("published") if isinstance(summary.get("published"), dict) else {}
    comments = []
    for item in published.get("comments_data", [])[:20]:
        if not isinstance(item, dict):
            continue
        comments.append({
            "content": str(item.get("content") or item.get("text") or ""),
            "like_count": optional_metric_int(item.get("like_count") or item.get("likes")),
            "time_text": str(item.get("create_time_text") or item.get("create_time") or ""),
        })
    topics = conn.execute(
        """
        SELECT topic, confidence
        FROM content_topics
        WHERE archive_id = ? AND confirmed = 1
        ORDER BY confidence DESC, topic COLLATE NOCASE
        LIMIT 10
        """,
        (row["id"],),
    ).fetchall()
    return {
        "archive_id": row["id"],
        "platform": next(iter(summary.get("feedback") or {}), ""),
        "title": summary.get("published_title") or summary.get("topic") or "",
        "description": str(summary.get("published_description") or summary.get("note") or "")[:4000],
        "author": summary.get("account_name") or str(published.get("author") or ""),
        "published_at": str(published.get("published_at") or ""),
        "published_url": stable_public_url(summary.get("published_url") or "", next(iter(summary.get("feedback") or {}), "")),
        "metrics": {
            platform: {
                key: values.get(key)
                for key in ANALYTICS_METRIC_FIELDS
            }
            for platform, values in (summary.get("feedback") or {}).items()
        },
        "metric_history": summary.get("metric_history", [])[:30],
        "comments_sample": comments,
        "confirmed_topics": [topic["topic"] for topic in topics],
        "allowed_archive_ids": [row["id"]],
    }


def account_analysis_payload(account_id: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    account = get_account_or_404(conn, account_id)
    metric_items = account_metric_rows(conn, account_id=account_id, limit=1)
    archive_rows = conn.execute(
        """
        SELECT DISTINCT a.*
        FROM archives a
        JOIN feedback f ON f.archive_id = a.id
        WHERE f.platform = ? AND f.account = ?
        ORDER BY a.archive_date DESC, a.id DESC
        LIMIT 50
        """,
        (account["platform"], account["account_name"]),
    ).fetchall()
    notes = []
    allowed_ids: List[str] = []
    for row in archive_rows:
        summary = archive_summary(row, conn)
        allowed_ids.append(row["id"])
        notes.append({
            "archive_id": row["id"],
            "title": summary.get("published_title") or summary.get("topic") or "",
            "description": str(summary.get("published_description") or summary.get("note") or "")[:1200],
            "published_at": str((summary.get("published") or {}).get("published_at") or ""),
            "metrics": summary.get("feedback") or {},
            "metric_history": summary.get("metric_history", [])[:10],
        })
    return {
        "account_id": account_id,
        "platform": account["platform"],
        "account_name": account["account_name"],
        "metrics": metric_items[0] if metric_items else {},
        "notes": notes,
        "allowed_archive_ids": allowed_ids,
    }


def topic_analysis_payload(topic: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    normalized = normalize_topic(topic)
    if not normalized:
        raise ValueError("topic is required")
    rows = conn.execute(
        """
        SELECT a.*, f.platform, f.account, f.views, f.likes, f.saves, f.comments, f.shares
        FROM content_topics ct
        JOIN archives a ON a.id = ct.archive_id
        LEFT JOIN feedback f ON f.archive_id = a.id
        WHERE ct.normalized_topic = ? AND ct.confirmed = 1
        ORDER BY a.archive_date DESC, a.id DESC
        LIMIT 100
        """,
        (normalized,),
    ).fetchall()
    notes: List[Dict[str, Any]] = []
    allowed_ids: List[str] = []
    for row in rows:
        allowed_ids.append(row["id"])
        notes.append({
            "archive_id": row["id"],
            "title": row["topic"],
            "platform": row["platform"],
            "account": row["account"],
            "published_date": row["archive_date"],
            "metrics": {key: row[key] for key in ANALYTICS_METRIC_FIELDS},
        })
    return {
        "topic": topic,
        "normalized_topic": normalized,
        "notes": notes,
        "allowed_archive_ids": allowed_ids,
    }


def queue_note_analysis(conn: sqlite3.Connection, archive_id: str, *, force: bool = False) -> Dict[str, Any]:
    config = load_ai_config()
    if not force and not config.configured:
        return {"queued": False, "reason": "AI_NOT_CONFIGURED", "job_id": ""}
    row = get_archive_or_404(conn, archive_id)
    return enqueue_ai_job(
        conn,
        kind="note_virality",
        scope_type="archive",
        scope_id=archive_id,
        payload=note_analysis_payload(row, conn),
        config=config,
    )


def queue_account_analysis(conn: sqlite3.Connection, account_id: str, *, force: bool = False) -> Dict[str, Any]:
    config = load_ai_config()
    if not force and not config.configured:
        return {"queued": False, "reason": "AI_NOT_CONFIGURED", "job_id": ""}
    return enqueue_ai_job(
        conn,
        kind="account_strategy",
        scope_type="account",
        scope_id=account_id,
        payload=account_analysis_payload(account_id, conn),
        config=config,
    )


def queue_account_analyses_for_items(
    conn: sqlite3.Connection,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for item in items:
        feedback = item.get("feedback") if isinstance(item.get("feedback"), dict) else {}
        platform = str(item.get("platform") or next(iter(feedback), "")).strip()
        account_name = str(item.get("account_name") or "").strip()
        key = (platform, account_name)
        if not platform or not account_name or key in seen:
            continue
        seen.add(key)
        row = conn.execute(
            "SELECT id FROM accounts WHERE platform = ? AND account_name = ? LIMIT 1",
            (platform, account_name),
        ).fetchone()
        if not row:
            account_id = create_account(
                conn,
                {
                    "platform": platform,
                    "account_name": account_name,
                    "enabled": True,
                },
                commit=False,
            )
        else:
            account_id = row["id"]
        results.append(queue_account_analysis(conn, account_id))
    return results


def queue_topic_analysis(conn: sqlite3.Connection, topic: str, *, force: bool = False) -> Dict[str, Any]:
    config = load_ai_config()
    payload = topic_analysis_payload(topic, conn)
    if len(payload["allowed_archive_ids"]) < 2:
        return {"queued": False, "reason": "TOPIC_SAMPLE_TOO_SMALL", "job_id": "", "sample_size": len(payload["allowed_archive_ids"])}
    if not force and not config.configured:
        return {"queued": False, "reason": "AI_NOT_CONFIGURED", "job_id": "", "sample_size": len(payload["allowed_archive_ids"])}
    return enqueue_ai_job(
        conn,
        kind="topic_competition",
        scope_type="topic",
        scope_id=payload["normalized_topic"],
        payload=payload,
        config=config,
    )


def get_archive_or_404(conn: sqlite3.Connection, archive_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
    if not row:
        abort(404, description="archive not found")
    return row


def create_archive(
    conn: sqlite3.Connection,
    payload: Dict[str, Any],
    commit: bool = True,
) -> str:
    archive_id = payload.get("id") or f"arc_{uuid.uuid4().hex[:12]}"
    topic = (payload.get("topic") or payload.get("title") or "").strip()
    if not topic:
        raise ValueError("topic is required")
    archive_date = (payload.get("archive_date") or payload.get("date") or datetime.now().date().isoformat()).strip()
    archive_time = (payload.get("archive_time") or payload.get("time") or datetime.now().strftime("%H:%M")).strip()
    favorite = 1 if payload.get("favorite") else 0
    note = (payload.get("note") or "").strip()
    published_url = (payload.get("published_url") or payload.get("url") or "").strip()
    published_snapshot_json = payload.get("published_snapshot_json")
    if isinstance(published_snapshot_json, dict):
        published_snapshot_json = json.dumps(published_snapshot_json, ensure_ascii=False)
    if not isinstance(published_snapshot_json, str):
        published_snapshot_json = '{}'
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO archives(id, topic, archive_date, archive_time, favorite, note, published_url, published_snapshot_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (archive_id, topic, archive_date, archive_time, favorite, note, published_url, published_snapshot_json, ts, ts),
    )
    if commit:
        conn.commit()
    return archive_id


def update_assets(
    conn: sqlite3.Connection,
    archive_id: str,
    cover_title: Optional[str],
    images: List[str],
    commit: bool = True,
) -> None:
    ts = now_iso()
    conn.execute("DELETE FROM assets WHERE archive_id = ?", (archive_id,))
    if cover_title:
        conn.execute(
            """
            INSERT INTO assets(id, archive_id, kind, asset_index, title, deleted, created_at, updated_at)
            VALUES (?, ?, 'cover', 0, ?, 0, ?, ?)
            """,
            (f"ast_{uuid.uuid4().hex[:12]}", archive_id, cover_title, ts, ts),
        )
    for idx, title in enumerate(images or [], start=1):
        conn.execute(
            """
            INSERT INTO assets(id, archive_id, kind, asset_index, title, deleted, created_at, updated_at)
            VALUES (?, ?, 'image', ?, ?, 0, ?, ?)
            """,
            (f"ast_{uuid.uuid4().hex[:12]}", archive_id, idx, title, ts, ts),
        )
    conn.execute("UPDATE archives SET updated_at = ? WHERE id = ?", (ts, archive_id))
    if commit:
        conn.commit()


def upsert_feedback(
    conn: sqlite3.Connection,
    archive_id: str,
    platform: str,
    metrics: Dict[str, Any],
    source: str = "manual",
    commit: bool = True,
) -> None:
    ts = now_iso()
    account = (metrics.get("account") or metrics.get("account_name") or "").strip()
    views = optional_metric_int(metrics.get("views"))
    likes = optional_metric_int(metrics.get("likes"))
    saves = optional_metric_int(metrics.get("saves"))
    comments = optional_metric_int(metrics.get("comments"))
    shares = optional_metric_int(metrics.get("shares"))
    existing = conn.execute(
        "SELECT id FROM feedback WHERE archive_id = ? AND platform = ?",
        (archive_id, platform),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE feedback
            SET account = ?, views = ?, likes = ?, saves = ?, comments = ?, shares = ?, source = ?, updated_at = ?
            WHERE archive_id = ? AND platform = ?
            """,
            (account, views, likes, saves, comments, shares, source, ts, archive_id, platform),
        )
    else:
        conn.execute(
            """
            INSERT INTO feedback(
                id, archive_id, platform, account, views, likes, saves, comments, shares, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"fb_{uuid.uuid4().hex[:12]}", archive_id, platform, account, views, likes, saves, comments, shares, source, ts, ts),
        )
    conn.execute("UPDATE archives SET updated_at = ? WHERE id = ?", (ts, archive_id))
    if commit:
        conn.commit()


def delete_asset(conn: sqlite3.Connection, archive_id: str, kind: str, index: int) -> None:
    row = conn.execute(
        "SELECT id FROM assets WHERE archive_id = ? AND kind = ? AND asset_index = ?",
        (archive_id, kind, index),
    ).fetchone()
    if not row:
        abort(404, description="asset not found")
    conn.execute("DELETE FROM assets WHERE id = ?", (row["id"],))
    conn.execute("UPDATE archives SET updated_at = ? WHERE id = ?", (now_iso(), archive_id))
    conn.commit()


def account_summary(row: sqlite3.Row, conn: sqlite3.Connection) -> Dict[str, Any]:
    platform = row["platform"]
    account_name = row["account_name"]
    content_count_row = conn.execute(
        "SELECT COUNT(DISTINCT archive_id) AS c FROM feedback WHERE platform = ? AND account = ?",
        (platform, account_name),
    ).fetchone()
    last_run = conn.execute(
        "SELECT * FROM sync_runs WHERE account_id = ? ORDER BY started_at DESC, finished_at DESC, id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    policy = connector_sync_policy(platform)
    return {
        "id": row["id"],
        "platform": platform,
        "account_name": account_name,
        "connector_key": row["connector_key"],
        "profile_url": row["profile_url"],
        "note": row["note"] or "",
        "enabled": bool(row["enabled"]),
        "sync_scope": policy["sync_scope"],
        "sync_label": policy["sync_label"],
        "sync_note": policy["sync_note"],
        "last_sync_at": row["last_sync_at"],
        "last_sync_status": row["last_sync_status"],
        "last_sync_error": row["last_sync_error"],
        "last_sync_cursor": row["last_sync_cursor"],
        "content_count": int((content_count_row["c"] if content_count_row else 0) or 0),
        "last_run": {
            "id": last_run["id"],
            "status": last_run["status"],
            "mode": last_run["mode"],
            "started_at": last_run["started_at"],
            "finished_at": last_run["finished_at"],
            "created_count": last_run["created_count"],
            "updated_count": last_run["updated_count"],
            "skipped_count": last_run["skipped_count"],
            "error": last_run["error"],
        } if last_run else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def get_account_or_404(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        abort(404, description="account not found")
    return row


def list_account_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT * FROM accounts ORDER BY updated_at DESC, created_at DESC").fetchall()
    return [account_summary(row, conn) for row in rows]


def normalized_topic(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().casefold())


def analytics_filter_clause(
    platform: str = "",
    account_id: str = "",
    alias: str = "f",
    topic: str = "",
) -> Tuple[str, List[Any]]:
    clauses = ["1=1"]
    params: List[Any] = []
    if platform:
        clauses.append(f"{alias}.platform = ?")
        params.append(platform)
    if account_id:
        clauses.append("a.id = ?")
        params.append(account_id)
    if topic:
        clauses.append(
            "EXISTS (SELECT 1 FROM content_topics ct "
            f"WHERE ct.archive_id = {alias}.archive_id AND ct.normalized_topic = ? AND ct.confirmed = 1)"
        )
        params.append(normalized_topic(topic))
    return " AND ".join(clauses), params


def metric_coverage(row: sqlite3.Row, notes_count: int) -> Dict[str, Dict[str, Optional[int]]]:
    return {
        field: {
            "value": row[f"sum_{field}"],
            "notes_with_value": row[f"count_{field}"],
            "notes_total": notes_count,
        }
        for field in ANALYTICS_METRIC_FIELDS
    }


def metric_totals(row: sqlite3.Row, notes_count: int) -> Dict[str, Any]:
    if notes_count == 0:
        return {
            "notes_count": 0,
            "views": None,
            "likes": None,
            "saves": None,
            "comments": None,
            "shares": None,
            "engagement": None,
            "metric_coverage": {
                field: {"value": None, "notes_with_value": 0, "notes_total": 0}
                for field in ANALYTICS_METRIC_FIELDS
            },
        }
    coverage = metric_coverage(row, notes_count)
    known_engagement = [
        coverage[field]["value"]
        for field in ENGAGEMENT_METRIC_FIELDS
        if coverage[field]["value"] is not None
    ]
    return {
        "notes_count": notes_count,
        "views": coverage["views"]["value"],
        "likes": coverage["likes"]["value"],
        "saves": coverage["saves"]["value"],
        "comments": coverage["comments"]["value"],
        "shares": coverage["shares"]["value"],
        "engagement": sum(known_engagement) if known_engagement else None,
        "metric_coverage": coverage,
    }


def account_metric_rows(
    conn: sqlite3.Connection,
    *,
    platform: str = "",
    account_id: str = "",
    topic: str = "",
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    where, params = analytics_filter_clause(platform, account_id, topic=topic)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params = [*params, max(1, int(limit))]
    aggregate_fields = ",\n".join(
        f"SUM(f.{field}) AS sum_{field}, COUNT(f.{field}) AS count_{field}"
        for field in ANALYTICS_METRIC_FIELDS
    )
    rows = conn.execute(
        f"""
        SELECT a.*, COUNT(DISTINCT f.archive_id) AS notes_count, {aggregate_fields}
        FROM accounts a
        LEFT JOIN feedback f ON f.platform = a.platform AND f.account = a.account_name
        WHERE {where}
        GROUP BY a.id
        ORDER BY notes_count DESC, a.account_name COLLATE NOCASE
        {limit_sql}
        """,
        params,
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        notes_count = int(row["notes_count"] or 0)
        items.append({
            "account_id": row["id"],
            "platform": row["platform"],
            "account_name": row["account_name"],
            "profile_url": stable_account_profile_url(row["profile_url"]),
            "enabled": bool(row["enabled"]),
            "last_sync_at": row["last_sync_at"],
            "last_sync_status": row["last_sync_status"],
            **metric_totals(row, notes_count),
        })
    return items


def analytics_scope_summary(
    conn: sqlite3.Connection,
    *,
    platform: str = "",
    account_id: str = "",
    topic: str = "",
) -> Dict[str, Any]:
    where, params = analytics_filter_clause(platform, account_id, topic=topic)
    aggregate_fields = ",\n".join(
        f"SUM(f.{field}) AS sum_{field}, COUNT(f.{field}) AS count_{field}"
        for field in ANALYTICS_METRIC_FIELDS
    )
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT f.archive_id) AS notes_count,
               COUNT(DISTINCT a.id) AS monitored_accounts,
               {aggregate_fields}
        FROM feedback f
        LEFT JOIN accounts a ON a.platform = f.platform AND a.account_name = f.account
        WHERE {where}
        """,
        params,
    ).fetchone()
    notes_count = int((row["notes_count"] if row else 0) or 0)
    return {
        **metric_totals(row, notes_count),
        "monitored_accounts": int((row["monitored_accounts"] if row else 0) or 0),
    }


def analytics_trend(
    conn: sqlite3.Connection,
    *,
    days: int = 14,
    platform: str = "",
    account_id: str = "",
    topic: str = "",
) -> List[Dict[str, Any]]:
    days = min(max(int(days or 14), 1), 90)
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days - 1)
    where, params = analytics_filter_clause(platform, account_id, topic=topic)
    rows = conn.execute(
        f"""
        SELECT ar.archive_date AS day,
               COUNT(DISTINCT ar.id) AS new_notes,
               SUM(COALESCE(f.likes, 0) + COALESCE(f.saves, 0) + COALESCE(f.comments, 0) + COALESCE(f.shares, 0)) AS engagement
        FROM archives ar
        JOIN feedback f ON f.archive_id = ar.id
        LEFT JOIN accounts a ON a.platform = f.platform AND a.account_name = f.account
        WHERE ar.archive_date >= ? AND ar.archive_date <= ? AND {where}
        GROUP BY ar.archive_date
        """,
        [start_date.isoformat(), end_date.isoformat(), *params],
    ).fetchall()
    by_day = {
        row["day"]: {
            "date": row["day"],
            "new_notes": int(row["new_notes"] or 0),
            "current_engagement": int(row["engagement"] or 0),
        }
        for row in rows
    }
    result: List[Dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        key = cursor.isoformat()
        result.append(by_day.get(key, {"date": key, "new_notes": 0, "current_engagement": 0}))
        cursor += timedelta(days=1)
    return result


def high_growth_items(
    conn: sqlite3.Connection,
    *,
    days: int = 14,
    platform: str = "",
    account_id: str = "",
    topic: str = "",
    limit: int = 10,
) -> List[Dict[str, Any]]:
    days = min(max(int(days or 14), 1), 90)
    limit = min(max(int(limit or 10), 1), 50)
    start_at = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    where, params = analytics_filter_clause(platform, account_id, alias="mh", topic=topic)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT mh.*,
                   COUNT(*) OVER (PARTITION BY mh.archive_id, mh.platform) AS sample_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY mh.archive_id, mh.platform
                       ORDER BY mh.captured_at ASC, mh.created_at ASC
                   ) AS first_rank,
                   ROW_NUMBER() OVER (
                       PARTITION BY mh.archive_id, mh.platform
                       ORDER BY mh.captured_at DESC, mh.created_at DESC
                   ) AS last_rank
            FROM metric_history mh
            JOIN archives ar ON ar.id = mh.archive_id
            LEFT JOIN accounts a ON a.platform = mh.platform AND a.account_name = (
                SELECT f.account FROM feedback f
                WHERE f.archive_id = mh.archive_id AND f.platform = mh.platform
                LIMIT 1
            )
            WHERE mh.captured_at >= ? AND {where}
        )
        SELECT ar.id AS archive_id, ar.topic, ar.published_url,
               f.account, f.platform, f.views, f.likes, f.saves, f.comments, f.shares,
               first.captured_at AS first_captured_at,
               last.captured_at AS last_captured_at,
               first.likes AS first_likes, last.likes AS last_likes,
               first.saves AS first_saves, last.saves AS last_saves,
               first.comments AS first_comments, last.comments AS last_comments,
               first.shares AS first_shares, last.shares AS last_shares
        FROM ranked first
        JOIN ranked last ON last.archive_id = first.archive_id
            AND last.platform = first.platform AND last.last_rank = 1
        JOIN archives ar ON ar.id = first.archive_id
        LEFT JOIN feedback f ON f.archive_id = first.archive_id AND f.platform = first.platform
        WHERE first.first_rank = 1
          AND first.sample_count >= 2
          AND (
              (first.likes IS NOT NULL AND last.likes IS NOT NULL) OR
              (first.saves IS NOT NULL AND last.saves IS NOT NULL) OR
              (first.comments IS NOT NULL AND last.comments IS NOT NULL) OR
              (first.shares IS NOT NULL AND last.shares IS NOT NULL)
          )
        ORDER BY (
            COALESCE(last.likes, first.likes, 0) - COALESCE(first.likes, 0) +
            COALESCE(last.saves, first.saves, 0) - COALESCE(first.saves, 0) +
            COALESCE(last.comments, first.comments, 0) - COALESCE(first.comments, 0) +
            COALESCE(last.shares, first.shares, 0) - COALESCE(first.shares, 0)
        ) DESC
        LIMIT ?
        """,
        [start_at, *params, limit],
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        first_known = [
            value for value in (row["first_likes"], row["first_saves"], row["first_comments"], row["first_shares"])
            if value is not None
        ]
        last_known = [
            value for value in (row["last_likes"], row["last_saves"], row["last_comments"], row["last_shares"])
            if value is not None
        ]
        first_engagement = sum(first_known) if first_known else None
        last_engagement = sum(last_known) if last_known else None
        delta = last_engagement - first_engagement if first_engagement is not None and last_engagement is not None else None
        items.append({
            "archive_id": row["archive_id"],
            "title": row["topic"],
            "published_url": stable_public_url(row["published_url"] or "", row["platform"] or ""),
            "platform": row["platform"],
            "account": row["account"] or "",
            "first_captured_at": row["first_captured_at"],
            "last_captured_at": row["last_captured_at"],
            "first_engagement": first_engagement,
            "last_engagement": last_engagement,
            "engagement_delta": delta,
            "engagement_growth_rate": (delta / first_engagement) if delta is not None and first_engagement else None,
            "current": {
                "views": row["views"],
                "likes": row["likes"],
                "saves": row["saves"],
                "comments": row["comments"],
                "shares": row["shares"],
            },
        })
    return items


def create_account(
    conn: sqlite3.Connection,
    payload: Dict[str, Any],
    commit: bool = True,
) -> str:
    account_id = payload.get("id") or f"acc_{uuid.uuid4().hex[:12]}"
    platform = (payload.get("platform") or "").strip()
    account_name = (payload.get("account_name") or payload.get("name") or "").strip()
    if not platform:
        raise ValueError("platform is required")
    if not account_name:
        raise ValueError("account_name is required")
    profile_url = (payload.get("profile_url") or "").strip()
    connector_key = (payload.get("connector_key") or resolve_connector(profile_url, platform).get("key") or resolve_connector("", platform).get("key") or "").strip()
    note = (payload.get("note") or "").strip()
    enabled = 1 if payload.get("enabled", True) else 0
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO accounts(id, platform, account_name, connector_key, profile_url, note, enabled, last_sync_at, last_sync_status, last_sync_error, last_sync_cursor, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (account_id, platform, account_name, connector_key, profile_url, note, enabled, "", "idle", "", "", ts, ts),
    )
    if commit:
        conn.commit()
    return account_id


def update_account(
    conn: sqlite3.Connection,
    account_id: str,
    payload: Dict[str, Any],
    commit: bool = True,
) -> None:
    row = get_account_or_404(conn, account_id)
    platform = (payload.get("platform") if payload.get("platform") is not None else row["platform"]).strip()
    account_name = (payload.get("account_name") if payload.get("account_name") is not None else row["account_name"]).strip()
    profile_url = (payload.get("profile_url") if payload.get("profile_url") is not None else row["profile_url"]).strip()
    connector_key = (payload.get("connector_key") if payload.get("connector_key") is not None else row["connector_key"]).strip()
    if not connector_key:
        connector_key = resolve_connector(profile_url, platform).get("key") or resolve_connector("", platform).get("key") or row["connector_key"]
    note = (payload.get("note") if payload.get("note") is not None else row["note"]) or ""
    enabled = row["enabled"] if payload.get("enabled") is None else (1 if payload.get("enabled") else 0)
    ts = now_iso()
    conn.execute(
        """
        UPDATE accounts
        SET platform = ?, account_name = ?, connector_key = ?, profile_url = ?, note = ?, enabled = ?, updated_at = ?
        WHERE id = ?
        """,
        (platform, account_name, connector_key, profile_url, note, enabled, ts, account_id),
    )
    if commit:
        conn.commit()


def delete_account(conn: sqlite3.Connection, account_id: str) -> None:
    get_account_or_404(conn, account_id)
    conn.execute("DELETE FROM sync_runs WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()


def seed_accounts_from_feedback() -> None:
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT platform, account FROM feedback WHERE account IS NOT NULL AND TRIM(account) != ''"
        ).fetchall()
        ts = now_iso()
        for row in rows:
            platform = row["platform"]
            account_name = row["account"].strip()
            connector_key = resolve_connector("", platform).get("key") or ""
            conn.execute(
                """
                INSERT OR IGNORE INTO accounts(id, platform, account_name, connector_key, profile_url, note, enabled, last_sync_at, last_sync_status, last_sync_error, last_sync_cursor, created_at, updated_at)
                VALUES (?, ?, ?, ?, '', '', 1, '', 'idle', '', '', ?, ?)
                """,
                (f"acc_{uuid.uuid5(uuid.NAMESPACE_URL, platform + '::' + account_name).hex[:12]}", platform, account_name, connector_key, ts, ts),
            )
        conn.commit()


def create_sync_run(
    conn: sqlite3.Connection,
    account_id: str,
    platform: str,
    mode: str,
    payload: Dict[str, Any],
) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO sync_runs(id, account_id, platform, mode, status, started_at, finished_at, created_count, updated_count, skipped_count, error, payload_json)
        VALUES (?, ?, ?, ?, 'running', ?, ?, 0, 0, 0, '', ?)
        """,
        (run_id, account_id, platform, mode, ts, ts, json.dumps(payload, ensure_ascii=False)),
    )
    return run_id


def finish_sync_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    created_count: int,
    updated_count: int,
    skipped_count: int,
    error: str = "",
) -> None:
    ts = now_iso()
    conn.execute(
        """
        UPDATE sync_runs
        SET status = ?, finished_at = ?, created_count = ?, updated_count = ?, skipped_count = ?, error = ?
        WHERE id = ?
        """,
        (status, ts, created_count, updated_count, skipped_count, error, run_id),
    )


def normalize_sync_post(post: Dict[str, Any], account: sqlite3.Row) -> Dict[str, Any]:
    snapshot = post.get("published_snapshot_json") or post.get("snapshot") or post.get("published_snapshot") or {}
    if isinstance(snapshot, str):
        snapshot = safe_json_loads(snapshot, {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    platform = (post.get("platform") or snapshot.get("platform") or account["platform"] or "").strip() or account["platform"]
    account_name = (post.get("account") or post.get("account_name") or snapshot.get("author") or account["account_name"] or "").strip() or account["account_name"]
    published_url = stable_public_url((post.get("published_url") or post.get("url") or post.get("link") or snapshot.get("url") or "").strip(), platform)
    title = (post.get("title") or post.get("topic") or snapshot.get("title") or snapshot.get("description") or account["account_name"] or "未命名内容").strip()
    note = (post.get("note") or snapshot.get("description") or snapshot.get("note") or "").strip()
    images = post.get("images") or snapshot.get("images") or []
    if not isinstance(images, list):
        images = []
    images = [str(item) for item in images if str(item).strip()]
    cover = (post.get("cover") or snapshot.get("cover") or (images[0] if images else "") or "").strip()
    metrics = post.get("metrics") or snapshot.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    else:
        metrics = dict(metrics)
    extra_metrics = snapshot.get("extra_metrics") if isinstance(snapshot.get("extra_metrics"), dict) else {}
    if metrics.get("shares") is None and extra_metrics.get("shares") is not None:
        metrics["shares"] = extra_metrics.get("shares")
    metrics.setdefault("account", account_name)
    collection_mode = (post.get("collection_mode") or snapshot.get("collection_mode") or connector_sync_policy(platform)["sync_scope"]).strip()
    if not collection_mode:
        collection_mode = connector_sync_policy(platform)["sync_scope"]
    captured_at = (snapshot.get("captured_at") or post.get("captured_at") or now_iso())
    published_date = (post.get("published_date") or snapshot.get("published_date") or captured_at[:10] or now_iso()[:10]).strip()
    published_time = (post.get("published_time") or snapshot.get("published_time") or captured_at[11:16] or now_iso()[11:16]).strip()
    if not snapshot:
        snapshot = {
            "url": published_url,
            "platform": platform,
            "connector": {
                "key": account["connector_key"] or resolve_connector(published_url, platform).get("key") or "generic",
                "name": resolve_connector(published_url, platform).get("name") or "通用网页连接器",
                "platform": platform,
            },
            "status": post.get("status") or "linked",
            "title": title,
            "description": note,
            "site_name": post.get("site_name") or "",
            "author": account_name,
            "cover": cover,
            "images": images,
            "metrics": metrics,
            "collection_mode": collection_mode,
            "error": "",
            "captured_at": now_iso(),
        }
    else:
        snapshot.setdefault("collection_mode", collection_mode)
        snapshot.setdefault("description", note)
        snapshot.setdefault("metrics", metrics)
        snapshot.setdefault("author", account_name)
    return {
        "platform": platform,
        "account": account_name,
        "published_url": published_url,
        "title": title,
        "cover": cover,
        "images": images,
        "metrics": metrics,
        "date": published_date,
        "time": published_time,
        "note": note,
        "collection_mode": collection_mode,
        "snapshot": snapshot,
        "source": post.get("source") or "account_sync",
    }


def upsert_archive_from_sync_post(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    post: Dict[str, Any],
) -> Dict[str, Any]:
    normalized = normalize_sync_post(post, account)
    validation_snapshot = normalized["snapshot"] if isinstance(normalized.get("snapshot"), dict) else {}
    validation_payload = {
        "platform": normalized["platform"],
        "title": validation_snapshot.get("title"),
        "author": validation_snapshot.get("author"),
        "cover": validation_snapshot.get("cover"),
        "images": validation_snapshot.get("images"),
        "metrics": validation_snapshot.get("metrics"),
    }
    missing_fields = required_capture_missing_fields(validation_payload)
    strict_status_invalid = (
        normalized["platform"] in STRICT_CAPTURE_PLATFORMS
        and validation_snapshot.get("status") != "fetched"
    )
    if missing_fields or strict_status_invalid:
        reason = validation_snapshot.get("error") or (
            "严格平台只允许真实抓取且完整的 fetched 快照" if strict_status_invalid else "强制字段缺失"
        )
        return {
            "action": "blocked",
            "missing_fields": missing_fields,
            "error": reason,
            "item": {
                "title": normalized["title"],
                "platform": normalized["platform"],
                "published_url": normalized["published_url"],
            },
        }
    published_url = normalized["published_url"]
    existing = None
    if published_url:
        existing = conn.execute("SELECT * FROM archives WHERE published_url = ?", (published_url,)).fetchone()
    archive_id = existing["id"] if existing else create_archive(
        conn,
        {
            "topic": normalized["title"],
            "archive_date": normalized["date"],
            "archive_time": normalized["time"],
            "note": normalized["note"],
            "published_url": published_url,
            "published_snapshot_json": normalized["snapshot"],
        },
        commit=False,
    )
    action = "updated" if existing else "created"
    if existing:
        conn.execute(
            """
            UPDATE archives
            SET topic = ?, archive_date = ?, archive_time = ?, note = ?, published_url = ?, published_snapshot_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                normalized["title"],
                normalized["date"],
                normalized["time"],
                normalized["note"],
                published_url,
                json.dumps(normalized["snapshot"], ensure_ascii=False),
                now_iso(),
                archive_id,
            ),
        )
    if normalized["cover"] or normalized["images"]:
        update_assets(conn, archive_id, normalized["cover"], normalized["images"], commit=False)
    upsert_feedback(
        conn,
        archive_id,
        normalized["platform"],
        normalized["metrics"],
        source=normalized["source"],
        commit=False,
    )
    record_metric_snapshot(
        conn,
        archive_id,
        normalized["platform"],
        normalized["metrics"],
        str((normalized["snapshot"] or {}).get("captured_at") or now_iso()),
        normalized["source"],
    )
    if action == "created":
        queue_note_analysis(conn, archive_id)
    row = get_archive_or_404(conn, archive_id)
    return {
        "action": action,
        "item": archive_summary(row, conn),
    }


def sync_account_posts(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    posts: List[Dict[str, Any]],
    mode: str = "manual",
    blocked_sources: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    blocked_sources = blocked_sources or []
    policy = connector_sync_policy(account["platform"])
    run_id = create_sync_run(
        conn,
        account["id"],
        account["platform"],
        mode,
        {"post_count": len(posts), "blocked_count": len(blocked_sources), "blocked_sources": blocked_sources, **policy},
    )
    created_count = 0
    updated_count = 0
    items: List[Dict[str, Any]] = []
    blocked_items: List[Dict[str, Any]] = [dict(item) for item in blocked_sources]
    try:
        for raw in posts:
            if not isinstance(raw, dict):
                blocked_items.append({"url": "", "error": "同步项不是对象"})
                continue
            result = upsert_archive_from_sync_post(conn, account, raw)
            if result["action"] == "blocked":
                blocked_items.append({
                    "url": (result.get("item") or {}).get("published_url") or "",
                    "title": (result.get("item") or {}).get("title") or "",
                    "error": result.get("error") or "强制字段缺失：" + "、".join(result.get("missing_fields") or []),
                    "missing_fields": result.get("missing_fields") or [],
                })
                continue
            if result["action"] == "updated":
                updated_count += 1
            else:
                created_count += 1
            items.append(result["item"])
        if not posts and not blocked_items:
            blocked_items.append({"url": account["profile_url"] or "", "error": "未发现可同步的真实账号内容"})
        skipped_count = len(blocked_items)
        success_count = created_count + updated_count
        if success_count and skipped_count:
            status = "partial"
        elif success_count:
            status = "success"
        else:
            status = "failed"
        error = ""
        if blocked_items:
            errors = [str(item.get("error") or "blocked") for item in blocked_items]
            error = f"{skipped_count} 条内容被阻塞：" + "；".join(dict.fromkeys(errors))
        run_payload = {
            "post_count": len(posts),
            "blocked_count": skipped_count,
            "blocked_sources": blocked_items,
            **policy,
        }
        conn.execute(
            "UPDATE sync_runs SET payload_json = ? WHERE id = ?",
            (json.dumps(run_payload, ensure_ascii=False), run_id),
        )
        finish_sync_run(conn, run_id, status, created_count, updated_count, skipped_count, error=error)
        conn.execute(
            """
            UPDATE accounts
            SET last_sync_at = ?, last_sync_status = ?, last_sync_error = ?, last_sync_cursor = '', updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), status, error, now_iso(), account["id"]),
        )
        return {
            "run_id": run_id,
            "status": status,
            "created_count": created_count,
            "updated_count": updated_count,
            "skipped_count": skipped_count,
            "blocked_count": skipped_count,
            "blocked_items": blocked_items,
            "items": items,
        }
    except Exception as exc:
        finish_sync_run(conn, run_id, "failed", created_count, updated_count, len(blocked_items), error=str(exc))
        conn.execute(
            """
            UPDATE accounts
            SET last_sync_at = ?, last_sync_status = ?, last_sync_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_iso(), 'failed', str(exc), now_iso(), account["id"]),
        )
        raise




# ----------------------------
# UI
# ----------------------------
@app.get("/")
def index():
    if FRONTEND_PATH.exists():
        return send_file(FRONTEND_PATH)
    return "frontend missing", 404


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return "", 204


def import_search_results(
    conn: sqlite3.Connection,
    platform: str,
    keyword: str,
    limit: int,
    request_id: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Search real platform results, rank by public likes, then persist fetched details."""
    platform_key = scraper_platform_key(platform)
    if not platform_key:
        raise ValueError("该平台暂不支持真实搜索")
    candidates, search_meta = scraper_service_search(platform_key, keyword, limit=min(max(limit * 2, limit), 50), request_id=request_id)

    def candidate_likes(item: Dict[str, Any]) -> int:
        stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        raw = first_optional_count_value(
            stats.get("like"), stats.get("likes"), item.get("like_count"), item.get("liked_count")
        )
        return raw if raw is not None else -1

    candidates = sorted(candidates, key=candidate_likes, reverse=True)
    items: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    for candidate in candidates[:limit]:
        if not isinstance(candidate, dict):
            continue
        candidate_url = first_text_value(candidate.get("url"), candidate.get("link"))
        content_id = first_text_value(candidate.get("content_id"), candidate.get("note_id"), candidate.get("photo_id"), candidate.get("aweme_id"))
        if not candidate_url and content_id:
            candidate_url = discovery_content_url(platform_key, candidate, content_id)
        if not candidate_url:
            failures.append({"url": "", "error": "搜索结果缺少稳定作品链接"})
            continue
        try:
            snapshot = capture_link_snapshot(candidate_url, platform_hint=platform, request_id=request_id)
            if snapshot.get("status") != "fetched":
                raise ValueError(snapshot.get("error") or "真实搜索结果详情抓取失败")
            snapshot = attach_real_comments(snapshot, candidate_url, request_id=request_id)
            archive_id, created = save_note_snapshot_with_status(
                conn,
                candidate_url,
                snapshot,
                favorite=False,
                source="keyword_search",
            )
            if created:
                queue_note_analysis(conn, archive_id)
            row = get_archive_or_404(conn, archive_id)
            items.append(archive_summary(row, conn))
        except Exception as exc:
            failures.append({"url": stable_public_url(candidate_url, platform), "error": redact_temporary_access_text(exc)})
    return items, failures


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "social-feedback-backend", "ts": now_iso()})


@app.get("/api/login-status")
def api_login_status():
    request_id = request.headers.get("X-Request-ID", "").strip()
    with connect_db() as conn:
        status = refresh_login_states(conn, request_id=request_id)
    return jsonify(status)


@app.post("/api/login/<platform>/open")
def api_open_login(platform: str):
    try:
        definition = login_platform_definition(platform)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    request_id = request.headers.get("X-Request-ID", "").strip()
    try:
        response = scraper_browser_action(f"/api/browser/login/{platform}", request_id=request_id)
        if not response.get("ok"):
            return jsonify({
                "ok": False,
                "platform": platform,
                "name": definition["name"],
                "error": redact_temporary_access_text(response.get("error") or "无法打开登录页"),
            }), 502
        logged_in = bool(response.get("logged_in"))
        with connect_db() as conn:
            update_login_state(conn, platform, logged_in=logged_in, error="")
            conn.commit()
        return jsonify({
            "ok": True,
            "platform": platform,
            "name": definition["name"],
            "logged_in": logged_in,
            "message": response.get("message") or ("已检测到登录态" if logged_in else "登录页已打开，请在可见 Chrome 中完成登录"),
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "platform": platform,
            "name": definition["name"],
            "error": redact_temporary_access_text(exc),
        }), 502


@app.post("/api/login/<platform>/confirm")
def api_confirm_login(platform: str):
    try:
        definition = login_platform_definition(platform)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    request_id = request.headers.get("X-Request-ID", "").strip()
    try:
        response = scraper_browser_action(f"/api/credentials/{platform}/browser", request_id=request_id)
        if not response.get("success") or not response.get("logged_in"):
            raise RuntimeError(response.get("message") or response.get("error") or "未检测到有效登录态")
        with connect_db() as conn:
            update_login_state(conn, platform, logged_in=True, confirmed=True, error="")
            conn.commit()
        with connect_db() as conn:
            status = refresh_login_states(conn, request_id=request_id)
        signal = status["signal"] if status.get("ok") else "PLATFORM_LOGIN_CONFIRMED"
        return jsonify({
            "ok": True,
            "platform": platform,
            "name": definition["name"],
            "confirmed": True,
            "message": f"{definition['name']}登录已确认，后续抓取将复用当前登录态",
            "signal": signal,
            "all_ready": status["all_ready"],
            "status": status,
        })
    except Exception as exc:
        message = redact_temporary_access_text(exc)
        with connect_db() as conn:
            update_login_state(conn, platform, logged_in=False, error=message)
            conn.commit()
        return jsonify({
            "ok": False,
            "platform": platform,
            "name": definition["name"],
            "confirmed": False,
            "error": f"{definition['name']}尚未确认：{message}",
        }), 409


def import_account_results(
    conn: sqlite3.Connection,
    platform: str,
    profile_url: str,
    limit: int,
    request_id: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]], Dict[str, Any]]:
    """Discover and persist real posts from one platform account URL."""
    platform_key = scraper_platform_key(platform)
    if not platform_key:
        raise ValueError("该平台暂不支持账号主页导入")
    account_profile_url = stable_account_profile_url(profile_url)
    account_import_mode = platform_key == "xhs"
    try:
        discovered, _ = scraper_service_account_contents(
            platform_key,
            profile_url,
            "",
            limit=limit,
            request_id=request_id,
        )
    except Exception as exc:
        message = redact_temporary_access_text(exc)
        if account_import_mode and is_interactive_verification_error(message):
            return [], [{"url": account_profile_url, "error": message}], {
                "status": "paused",
                "successful_count": 0,
                "remaining_count": limit,
                "pause_reason": "小红书账号主页要求人工交互验证，账号导入已暂停",
                "resumable": True,
            }
        raise

    def candidate_publish_time(item: Dict[str, Any]) -> int:
        raw = first_text_value(
            item.get("publish_time"), item.get("published_at"), item.get("create_time"), item.get("timestamp")
        )
        return coerce_timestamp_seconds(raw) or 0

    discovered = [item for item in discovered if isinstance(item, dict)]
    if not account_import_mode:
        discovered = sorted(discovered, key=candidate_publish_time, reverse=True)
    selected = discovered[:limit]
    items: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    seen: set[str] = set()
    paused_at_index: int | None = None
    for candidate_index, candidate in enumerate(selected):
        content_id = first_text_value(candidate.get("content_id"), candidate.get("note_id"), candidate.get("photo_id"), candidate.get("aweme_id"))
        capture_url = first_text_value(candidate.get("url"), candidate.get("link"))
        if not capture_url and content_id:
            capture_url = discovery_content_url(platform_key, candidate, content_id)
        stable_url = stable_public_url(capture_url, platform)
        if account_import_mode:
            capture_url = stable_url
        if not stable_url or stable_url in seen:
            continue
        seen.add(stable_url)
        try:
            snapshot = capture_link_snapshot(
                capture_url,
                platform_hint=platform,
                request_id=request_id,
                account_import=account_import_mode,
            )
            if snapshot.get("status") != "fetched":
                raise ValueError(snapshot.get("error") or "账号作品详情抓取失败")
            snapshot = attach_real_comments(
                snapshot,
                capture_url,
                request_id=request_id,
                account_import=account_import_mode,
            )
            snapshot["import_context"] = {
                "source": "account_import",
                "account_name": first_text_value(snapshot.get("author"), candidate.get("author")),
                "account_profile_url": account_profile_url,
            }
            archive_id, created = save_note_snapshot_with_status(
                conn,
                capture_url,
                snapshot,
                favorite=False,
                source="account_import",
            )
            if created:
                queue_note_analysis(conn, archive_id)
            row = get_archive_or_404(conn, archive_id)
            items.append(archive_summary(row, conn))
        except Exception as exc:
            message = redact_temporary_access_text(exc)
            failures.append({"url": stable_url, "error": message})
            if account_import_mode and is_interactive_verification_error(message):
                paused_at_index = candidate_index
                break
    if not discovered:
        failures.append({"url": account_profile_url, "error": "未从该账号主页发现可导入的真实作品"})
    paused = paused_at_index is not None
    if paused:
        progress_status = "paused"
    elif failures and items:
        progress_status = "partial"
    elif failures:
        progress_status = "failed"
    else:
        progress_status = "completed"
    progress = {
        "status": progress_status,
        "successful_count": len(items),
        "remaining_count": max(1, len(selected) - paused_at_index) if paused_at_index is not None else 0,
        "pause_reason": "小红书要求人工交互验证，账号导入已暂停" if paused else "",
        "resumable": paused,
    }
    return items, failures, progress


@app.post("/api/notes/account-import")
def api_account_import_notes():
    payload = request.get_json(force=True, silent=False) or {}
    profile_url = str(payload.get("profile_url") or payload.get("url") or "").strip()
    platform = str(payload.get("platform") or detect_platform_from_url(profile_url)).strip()
    if not profile_url.startswith(("http://", "https://")):
        return jsonify({"error": "请输入有效的账号主页链接"}), 400
    if platform not in PLATFORMS or not scraper_platform_key(platform):
        return jsonify({"error": "请选择支持账号主页导入的平台"}), 400
    try:
        limit = min(max(int(payload.get("limit") or 20), 1), 50)
    except (TypeError, ValueError):
        return jsonify({"error": "获取数量必须是 1 到 50 的整数"}), 400
    request_id = request.headers.get("X-Request-ID", "").strip()
    with connect_db() as conn:
        try:
            items, failures, progress = import_account_results(
                conn, platform, profile_url, limit, request_id=request_id
            )
            account_ai_jobs = queue_account_analyses_for_items(conn, items)
            conn.commit()
            return jsonify({
                "items": items,
                "failures": failures,
                "platform": platform,
                "limit": limit,
                "ai_jobs": account_ai_jobs,
                **progress,
            })
        except Exception as exc:
            conn.rollback()
            message = redact_temporary_access_text(exc)
            logger.warning("account import failed (%s): %s", platform, message)
            return jsonify({"error": message, "items": [], "failures": []}), 502


@app.post("/api/notes/search-import")
def api_search_import_notes():
    payload = request.get_json(force=True, silent=False) or {}
    platform = str(payload.get("platform") or "").strip()
    keyword = str(payload.get("keyword") or "").strip()
    if platform not in PLATFORMS or not scraper_platform_key(platform):
        return jsonify({"error": "请选择支持真实搜索的平台"}), 400
    if not keyword:
        return jsonify({"error": "请输入搜索关键词"}), 400
    try:
        limit = min(max(int(payload.get("limit") or 20), 1), 50)
    except (TypeError, ValueError):
        return jsonify({"error": "搜索数量必须是 1 到 50 的整数"}), 400
    request_id = request.headers.get("X-Request-ID", "").strip()
    with connect_db() as conn:
        try:
            items, failures = import_search_results(conn, platform, keyword, limit, request_id=request_id)
            account_ai_jobs = queue_account_analyses_for_items(conn, items)
            conn.commit()
            return jsonify({
                "items": items,
                "failures": failures,
                "platform": platform,
                "keyword": keyword,
                "limit": limit,
                "ai_jobs": account_ai_jobs,
            })
        except Exception as exc:
            conn.rollback()
            message = redact_temporary_access_text(exc)
            logger.warning("search import failed (%s): %s", platform, message)
            return jsonify({"error": message, "items": [], "failures": []}), 502




@app.get("/api/platforms")
def get_platforms():
    return jsonify({"platforms": PLATFORMS})


@app.get("/api/connectors")
def get_connectors():
    return jsonify({"connectors": list_connectors()})


@app.get("/api/accounts")
def list_accounts():
    with connect_db() as conn:
        return jsonify({"accounts": list_account_rows(conn)})


@app.get("/api/accounts/metrics")
def list_account_metrics():
    platform = (request.args.get("platform") or "").strip()
    account_id = (request.args.get("account_id") or "").strip()
    try:
        limit = min(max(int(request.args.get("limit") or 200), 1), 500)
    except (TypeError, ValueError):
        return jsonify({"error": "limit 必须是整数"}), 400
    with connect_db() as conn:
        items = account_metric_rows(conn, platform=platform, account_id=account_id, limit=limit)
    return jsonify({"items": items, "filters": {"platform": platform, "account_id": account_id}})


@app.get("/api/accounts/<account_id>/metrics")
def get_account_metrics(account_id: str):
    try:
        days = min(max(int(request.args.get("days") or 14), 1), 90)
    except (TypeError, ValueError):
        return jsonify({"error": "days 必须是整数"}), 400
    with connect_db() as conn:
        row = get_account_or_404(conn, account_id)
        metric_items = account_metric_rows(conn, account_id=account_id, limit=1)
        metrics = metric_items[0] if metric_items else None
        return jsonify({
            "account": account_summary(row, conn),
            "metrics": metrics,
            "trend": analytics_trend(conn, days=days, account_id=account_id),
        })


@app.get("/api/dashboard/high-growth")
def get_dashboard_high_growth():
    try:
        days = min(max(int(request.args.get("days") or 14), 1), 90)
        limit = min(max(int(request.args.get("limit") or 10), 1), 50)
    except (TypeError, ValueError):
        return jsonify({"error": "days 和 limit 必须是整数"}), 400
    platform = (request.args.get("platform") or "").strip()
    account_id = (request.args.get("account_id") or "").strip()
    topic = (request.args.get("topic") or "").strip()
    with connect_db() as conn:
        items = high_growth_items(
            conn,
            days=days,
            platform=platform,
            account_id=account_id,
            topic=topic,
            limit=limit,
        )
    return jsonify({"items": items, "filters": {"days": days, "platform": platform, "account_id": account_id, "topic": topic}})


@app.get("/api/dashboard/overview")
def get_dashboard_overview():
    try:
        days = min(max(int(request.args.get("days") or 14), 1), 90)
    except (TypeError, ValueError):
        return jsonify({"error": "days 必须是整数"}), 400
    platform = (request.args.get("platform") or "").strip()
    account_id = (request.args.get("account_id") or "").strip()
    topic = (request.args.get("topic") or "").strip()
    with connect_db() as conn:
        if account_id:
            get_account_or_404(conn, account_id)
        summary = analytics_scope_summary(conn, platform=platform, account_id=account_id, topic=topic)
        trend = analytics_trend(conn, days=days, platform=platform, account_id=account_id, topic=topic)
        high_growth = high_growth_items(
            conn,
            days=days,
            platform=platform,
            account_id=account_id,
            topic=topic,
            limit=10,
        )
        accounts = account_metric_rows(
            conn,
            platform=platform,
            account_id=account_id,
            topic=topic,
            limit=10,
        )
        where, params = analytics_filter_clause(platform, account_id, topic=topic)
        platform_rows = conn.execute(
            f"""
            SELECT f.platform, COUNT(DISTINCT f.archive_id) AS notes_count,
                   SUM(COALESCE(f.likes, 0) + COALESCE(f.saves, 0) + COALESCE(f.comments, 0) + COALESCE(f.shares, 0)) AS engagement
            FROM feedback f
            LEFT JOIN accounts a ON a.platform = f.platform AND a.account_name = f.account
            WHERE {where}
            GROUP BY f.platform
            ORDER BY engagement DESC
            """,
            params,
        ).fetchall()
        topic_rows = conn.execute(
            """
            SELECT ct.topic, ct.normalized_topic, COUNT(DISTINCT ct.archive_id) AS notes_count
            FROM content_topics ct
            WHERE ct.confirmed = 1
            GROUP BY ct.normalized_topic
            ORDER BY notes_count DESC, ct.topic COLLATE NOCASE
            LIMIT 100
            """
        ).fetchall()
        confirmed_topics = [dict(row) for row in topic_rows]
        platform_distribution = [
            {
                "platform": row["platform"],
                "notes_count": int(row["notes_count"] or 0),
                "engagement": int(row["engagement"] or 0),
            }
            for row in platform_rows
        ]
    return jsonify({
        "filters": {"days": days, "platform": platform, "account_id": account_id, "topic": topic},
        "kpi": {
            **summary,
            "recent_new_notes": sum(item["new_notes"] for item in trend),
            "recent_current_engagement": sum(item["current_engagement"] for item in trend),
        },
        "trend": trend,
        "high_growth": high_growth,
        "accounts": accounts,
        "platform_distribution": platform_distribution,
        "topics": confirmed_topics,
    })


@app.get("/api/ai/status")
def api_ai_status():
    with connect_db() as conn:
        queued = conn.execute("SELECT COUNT(*) AS c FROM ai_jobs WHERE status IN ('queued', 'running')").fetchone()["c"]
        recent = conn.execute(
            "SELECT status, COUNT(*) AS c FROM ai_jobs WHERE queued_at >= ? GROUP BY status",
            ((datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),),
        ).fetchall()
    status = load_ai_config().public_status()
    status["queued_jobs"] = int(queued or 0)
    status["last_24h"] = {row["status"]: int(row["c"] or 0) for row in recent}
    return jsonify(status)


def ai_insight_payload(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "kind": row["kind"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "result": safe_json_loads(row["result_json"] or "{}", {}),
        "evidence": safe_json_loads(row["evidence_json"] or "[]", []),
        "provider": row["provider"],
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "generated_at": row["generated_at"],
    }


def latest_insight(conn: sqlite3.Connection, kind: str, scope_type: str, scope_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT * FROM ai_insights
        WHERE kind = ? AND scope_type = ? AND scope_id = ?
        ORDER BY generated_at DESC LIMIT 1
        """,
        (kind, scope_type, scope_id),
    ).fetchone()
    return ai_insight_payload(row) if row else None


@app.get("/api/ai/jobs/<job_id>")
def api_ai_job(job_id: str):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM ai_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            insight = conn.execute("SELECT * FROM ai_insights WHERE id = ?", (job_id,)).fetchone()
            if insight:
                return jsonify({"ok": True, "status": "success", "insight": ai_insight_payload(insight)})
            return jsonify({"error": "AI job not found"}), 404
        insight = conn.execute("SELECT * FROM ai_insights WHERE job_id = ?", (row["id"],)).fetchone()
        return jsonify({
            "ok": row["status"] != "failed",
            "job": {
                "id": row["id"],
                "kind": row["kind"],
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "status": row["status"],
                "attempts": row["attempts"],
                "provider": row["provider"],
                "model": row["model"],
                "queued_at": row["queued_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "error": row["error"],
            },
            "insight": ai_insight_payload(insight) if insight else None,
        })


@app.post("/api/archives/<archive_id>/insight")
def api_queue_archive_insight(archive_id: str):
    with connect_db() as conn:
        result = queue_note_analysis(conn, archive_id, force=True)
        conn.commit()
    return jsonify(result), 202 if result.get("queued") else 200


@app.get("/api/archives/<archive_id>/insight")
def api_archive_insight(archive_id: str):
    with connect_db() as conn:
        get_archive_or_404(conn, archive_id)
        insight = latest_insight(conn, "note_virality", "archive", archive_id)
    return jsonify({"insight": insight})


@app.post("/api/accounts/<account_id>/insight")
def api_queue_account_insight(account_id: str):
    with connect_db() as conn:
        get_account_or_404(conn, account_id)
        result = queue_account_analysis(conn, account_id, force=True)
        conn.commit()
    return jsonify(result), 202 if result.get("queued") else 200


@app.get("/api/accounts/<account_id>/insight")
def api_account_insight(account_id: str):
    with connect_db() as conn:
        get_account_or_404(conn, account_id)
        insight = latest_insight(conn, "account_strategy", "account", account_id)
    return jsonify({"insight": insight})


@app.get("/api/archives/<archive_id>/topics")
def api_archive_topics(archive_id: str):
    with connect_db() as conn:
        get_archive_or_404(conn, archive_id)
        rows = conn.execute(
            """
            SELECT * FROM content_topics
            WHERE archive_id = ?
            ORDER BY confirmed DESC, confidence DESC, topic COLLATE NOCASE
            """,
            (archive_id,),
        ).fetchall()
    return jsonify({"topics": [dict(row) for row in rows]})


@app.patch("/api/content-topics/<topic_id>")
def api_patch_content_topic(topic_id: str):
    payload = request.get_json(force=True, silent=False) or {}
    topic = str(payload.get("topic") or "").strip()
    if payload.get("confirmed") is not None and not isinstance(payload.get("confirmed"), bool):
        return jsonify({"error": "confirmed 必须是布尔值"}), 400
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM content_topics WHERE id = ?", (topic_id,)).fetchone()
        if not row:
            return jsonify({"error": "topic not found"}), 404
        next_topic = topic or row["topic"]
        next_normalized = normalize_topic(next_topic)
        if not next_normalized:
            return jsonify({"error": "topic 不能为空"}), 400
        confirmed = bool(payload.get("confirmed", row["confirmed"]))
        ts = now_iso()
        try:
            conn.execute(
                """
                UPDATE content_topics
                SET topic = ?, normalized_topic = ?, source = 'manual', confirmed = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_topic, next_normalized, 1 if confirmed else 0, ts, topic_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return jsonify({"error": "该笔记已经存在同名赛道标签"}), 409
        result = {"ok": True}
        if confirmed:
            sample = conn.execute(
                "SELECT COUNT(*) AS c FROM content_topics WHERE normalized_topic = ? AND confirmed = 1",
                (next_normalized,),
            ).fetchone()["c"]
            result["topic_analysis"] = (
                queue_topic_analysis(conn, next_topic)
                if int(sample or 0) >= 2
                else {
                    "queued": False,
                    "reason": "TOPIC_SAMPLE_TOO_SMALL",
                    "job_id": "",
                    "sample_size": int(sample or 0),
                }
            )
        updated = conn.execute("SELECT * FROM content_topics WHERE id = ?", (topic_id,)).fetchone()
        conn.commit()
    return jsonify({**result, "topic": dict(updated)})


@app.post("/api/topics/<path:topic>/insight")
def api_queue_topic_insight(topic: str):
    with connect_db() as conn:
        result = queue_topic_analysis(conn, topic, force=True)
        conn.commit()
    return jsonify(result), 202 if result.get("queued") else 200


@app.get("/api/topics/<path:topic>/insight")
def api_topic_insight(topic: str):
    with connect_db() as conn:
        insight = latest_insight(conn, "topic_competition", "topic", normalize_topic(topic))
    return jsonify({"insight": insight})


@app.post("/api/accounts")
def api_create_account():
    payload = request.get_json(force=True, silent=False) or {}
    with connect_db() as conn:
        try:
            account_id = create_account(conn, payload)
            row = get_account_or_404(conn, account_id)
            conn.commit()
            return jsonify({"item": account_summary(row, conn)}), 201
        except ValueError as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 400
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            return jsonify({"error": f"account already exists or conflicts: {exc}"}), 400


@app.patch("/api/accounts/<account_id>")
def api_patch_account(account_id: str):
    payload = request.get_json(force=True, silent=False) or {}
    with connect_db() as conn:
        try:
            update_account(conn, account_id, payload)
            row = get_account_or_404(conn, account_id)
            conn.commit()
            return jsonify({"item": account_summary(row, conn)})
        except ValueError as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 400


@app.delete("/api/accounts/<account_id>")
def api_delete_account(account_id: str):
    with connect_db() as conn:
        delete_account(conn, account_id)
        conn.commit()
    return jsonify({"ok": True})


@app.post("/api/accounts/<account_id>/sync")
def api_sync_account(account_id: str):
    payload = request.get_json(force=True, silent=False) or {}
    source_urls = payload.get("source_urls") or []
    posts = payload.get("posts") or []
    mode = (payload.get("mode") or "manual").strip() or "manual"
    try:
        limit = min(max(int(payload.get("limit") or 20), 1), 50)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    if not isinstance(source_urls, list):
        return jsonify({"error": "source_urls must be a list"}), 400
    if not isinstance(posts, list):
        return jsonify({"error": "posts must be a list"}), 400
    request_id = request.headers.get("X-Request-ID", "").strip()
    with connect_db() as conn:
        account = get_account_or_404(conn, account_id)
        prepared_posts = [post for post in posts if isinstance(post, dict)]
        blocked_sources: List[Dict[str, Any]] = []
        profile_identifiers: List[str] = []
        for raw_url in source_urls:
            url = str(raw_url or "").strip()
            if not url:
                continue
            context = extract_content_context(url, account["platform"])
            if account["platform"] in STRICT_CAPTURE_PLATFORMS and not context.get("content_id"):
                profile_identifiers.append(url)
                continue
            snapshot = capture_link_snapshot(url, account["platform"], request_id)
            if snapshot.get("status") != "fetched":
                blocked_sources.append({"url": url, "error": snapshot.get("error") or snapshot.get("status") or "blocked"})
                continue
            prepared_posts.append({
                "published_url": url,
                "platform": snapshot.get("platform") or account["platform"],
                "account": account["account_name"],
                "title": snapshot.get("title") or snapshot.get("description") or account["account_name"],
                "cover": snapshot.get("cover") or "",
                "images": snapshot.get("images") or [],
                "metrics": snapshot.get("metrics") or {},
                "published_date": snapshot.get("published_date") or snapshot.get("captured_at", now_iso())[:10],
                "published_time": snapshot.get("published_time") or snapshot.get("captured_at", now_iso())[11:16],
                "note": snapshot.get("description") or "",
                "published_snapshot_json": snapshot,
                "source": "link_capture",
            })

        should_discover = account["platform"] in STRICT_CAPTURE_PLATFORMS and (
            bool(profile_identifiers)
            or (not source_urls and not posts)
            or (not prepared_posts and bool(account["profile_url"]))
        )
        if should_discover:
            identifier = first_text_value(
                profile_identifiers[0] if profile_identifiers else "",
                account["profile_url"],
            )
            platform_key = scraper_platform_key(account["platform"])
            try:
                discovered, discovery_meta = scraper_service_account_contents(
                    platform_key,
                    identifier,
                    account["account_name"],
                    limit=limit,
                    request_id=request_id,
                )
                discovered_posts, detail_blocks = prepare_account_discovered_posts(
                    account,
                    discovered,
                    discovery_meta,
                    request_id,
                )
                prepared_posts.extend(discovered_posts)
                blocked_sources.extend(detail_blocks)
                if not discovered:
                    blocked_sources.append({
                        "url": identifier if identifier.startswith(("http://", "https://")) else "",
                        "error": f"未从真实账号身份发现作品：{account['account_name']}",
                    })
                mode = "account_discovery" if mode == "manual" else mode
            except Exception as exc:
                blocked_sources.append({
                    "url": identifier if identifier.startswith(("http://", "https://")) else "",
                    "error": f"真实账号作品发现失败：{exc}",
                })

        if not prepared_posts and not blocked_sources and account["profile_url"]:
            if account["platform"] in STRICT_CAPTURE_PLATFORMS:
                blocked_sources.append({"url": account["profile_url"], "error": "严格平台账号主页未产生任何真实作品详情"})
            else:
                snapshot = capture_link_snapshot(account["profile_url"], account["platform"], request_id)
                if snapshot.get("status") == "fetched":
                    prepared_posts.append({
                        "published_url": account["profile_url"],
                        "platform": snapshot.get("platform") or account["platform"],
                        "account": account["account_name"],
                        "title": snapshot.get("title") or account["account_name"],
                        "cover": snapshot.get("cover") or "",
                        "images": snapshot.get("images") or [],
                        "metrics": snapshot.get("metrics") or {},
                        "published_date": snapshot.get("published_date") or snapshot.get("captured_at", now_iso())[:10],
                        "published_time": snapshot.get("published_time") or snapshot.get("captured_at", now_iso())[11:16],
                        "note": snapshot.get("description") or "",
                        "published_snapshot_json": snapshot,
                        "source": "profile_capture",
                    })
                else:
                    blocked_sources.append({"url": account["profile_url"], "error": snapshot.get("error") or snapshot.get("status") or "blocked"})
        try:
            result = sync_account_posts(conn, account, prepared_posts, mode=mode, blocked_sources=blocked_sources)
            conn.commit()
            row = get_account_or_404(conn, account_id)
            return jsonify({
                "account": account_summary(row, conn),
                "result": result,
                "blocked_sources": result.get("blocked_items") or [],
            })
        except Exception as exc:
            conn.rollback()
            row = get_account_or_404(conn, account_id)
            return jsonify({"error": str(exc), "account": account_summary(row, conn)}), 500


@app.get("/api/sync-runs")
def list_sync_runs():
    account_id = (request.args.get("account_id") or "").strip()
    limit = min(max(int(request.args.get("limit") or 50), 1), 200)
    with connect_db() as conn:
        if account_id:
            rows = conn.execute(
                "SELECT * FROM sync_runs WHERE account_id = ? ORDER BY started_at DESC, finished_at DESC, id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sync_runs ORDER BY started_at DESC, finished_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = [{
            "id": row["id"],
            "account_id": row["account_id"],
            "platform": row["platform"],
            "mode": row["mode"],
            "status": row["status"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "created_count": row["created_count"],
            "updated_count": row["updated_count"],
            "skipped_count": row["skipped_count"],
            "blocked_count": int((safe_json_loads(row["payload_json"], {}) or {}).get("blocked_count") or 0),
            "error": row["error"],
        } for row in rows]
    return jsonify({"items": items})


@app.get("/api/archives")
def list_archives():
    q = (request.args.get("q") or "").strip().lower()
    date = (request.args.get("date") or "").strip()
    favorite = request.args.get("favorite")
    with connect_db() as conn:
        rows = conn.execute("SELECT * FROM archives ORDER BY archive_date DESC, archive_time DESC, created_at DESC").fetchall()
        items = [archive_summary(r, conn) for r in rows]
    if date:
        items = [r for r in items if r["date"] == date]
    if q:
        items = [
            r for r in items
            if q in (r.get("topic") or "").lower()
            or q in (r.get("original_url") or "").lower()
            or q in ((r.get("published") or {}).get("title") or "").lower()
            or any(
                q in (platform or "").lower() or q in (fb.get("account") or "").lower()
                for platform, fb in (r.get("feedback") or {}).items()
            )
        ]
    if favorite is not None:
        want = favorite.lower() in {"1", "true", "yes", "on"}
        items = [r for r in items if r["favorite"] == want]
    return jsonify({"items": items})


@app.get("/api/archives/<archive_id>")
def get_archive(archive_id: str):
    with connect_db() as conn:
        row = get_archive_or_404(conn, archive_id)
        return jsonify(archive_summary(row, conn))


@app.post("/api/notes/import")
def api_import_notes():
    payload = request.get_json(force=True, silent=False) or {}
    raw_urls = payload.get("urls") or []
    if isinstance(raw_urls, str):
        raw_urls = raw_urls.splitlines()
    if not isinstance(raw_urls, list):
        return jsonify({"error": "urls must be a list or newline-separated string"}), 400
    urls: List[str] = []
    seen: set[str] = set()
    for value in raw_urls:
        url = str(value or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    if not urls:
        return jsonify({"error": "请至少填写一条真实笔记链接"}), 400
    if len(urls) > 20:
        return jsonify({"error": "一次最多添加 20 条笔记链接"}), 400

    favorite = bool(payload.get("favorite"))
    request_id = request.headers.get("X-Request-ID", "").strip()
    items: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    with connect_db() as conn:
        for url in urls:
            try:
                snapshot = capture_link_snapshot(url, request_id=request_id)
                if snapshot.get("status") != "fetched":
                    raise ValueError(snapshot.get("error") or "真实笔记抓取未成功")
                snapshot = attach_real_comments(snapshot, url, request_id=request_id)
                archive_id, created = save_note_snapshot_with_status(
                    conn,
                    url,
                    snapshot,
                    favorite=favorite,
                    source="link_import",
                )
                if created:
                    queue_note_analysis(conn, archive_id)
                conn.commit()
                row = get_archive_or_404(conn, archive_id)
                items.append(archive_summary(row, conn))
            except Exception as exc:
                conn.rollback()
                failures.append({"url": stable_public_url(url, detect_platform_from_url(url)), "error": redact_temporary_access_text(exc)})
        account_ai_jobs = queue_account_analyses_for_items(conn, items)
        conn.commit()
    return jsonify({"items": items, "failures": failures, "ai_jobs": account_ai_jobs})


@app.post("/api/archives/<archive_id>/refresh")
def api_refresh_archive(archive_id: str):
    request_id = request.headers.get("X-Request-ID", "").strip()
    with connect_db() as conn:
        row = get_archive_or_404(conn, archive_id)
        published = safe_json_loads(row["published_snapshot_json"] or "{}", {})
        url = first_text_value(row["published_url"], published.get("url") if isinstance(published, dict) else "")
        if not url:
            return jsonify({"error": "这条笔记没有可更新的原文链接"}), 400
        try:
            snapshot = capture_link_snapshot(url, request_id=request_id)
            if snapshot.get("status") != "fetched":
                return jsonify({"error": snapshot.get("error") or "真实笔记抓取未成功"}), 422
            snapshot = attach_real_comments(snapshot, url, request_id=request_id)
            saved_id = save_note_snapshot(conn, url, snapshot, favorite=None, source="refresh")
            conn.commit()
            saved_row = get_archive_or_404(conn, saved_id)
            return jsonify({"item": archive_summary(saved_row, conn)})
        except Exception as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 500


@app.post("/api/archives")
def api_create_archive():
    blocked = legacy_write_guard()
    if blocked:
        return blocked
    payload = request.get_json(force=True, silent=False) or {}
    with connect_db() as conn:
        try:
            archive_id = create_archive(conn, payload)
            update_assets(conn, archive_id, payload.get("cover"), payload.get("images") or [])
            for platform, metrics in (payload.get("feedback") or {}).items():
                if platform in PLATFORMS:
                    upsert_feedback(conn, archive_id, platform, metrics or {}, source=payload.get("source", "manual"))
            row = get_archive_or_404(conn, archive_id)
            return jsonify({"item": archive_summary(row, conn)}), 201
        except ValueError as exc:
            conn.rollback()
            return jsonify({"error": str(exc)}), 400


@app.patch("/api/archives/<archive_id>")
def api_patch_archive(archive_id: str):
    payload = request.get_json(force=True, silent=False) or {}
    with connect_db() as conn:
        row = get_archive_or_404(conn, archive_id)
        topic = (payload.get("topic") or row["topic"]).strip()
        archive_date = (payload.get("archive_date") or row["archive_date"]).strip()
        archive_time = (payload.get("archive_time") or row["archive_time"]).strip()
        favorite = row["favorite"] if payload.get("favorite") is None else (1 if payload.get("favorite") else 0)
        note = (payload.get("note") if payload.get("note") is not None else row["note"]) or ""
        published_url = (payload.get("published_url") if payload.get("published_url") is not None else row["published_url"]) if "published_url" in row.keys() else (payload.get("published_url") or "")
        ts = now_iso()
        conn.execute(
            """
            UPDATE archives
            SET topic = ?, archive_date = ?, archive_time = ?, favorite = ?, note = ?, published_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (topic, archive_date, archive_time, favorite, note, published_url, ts, archive_id),
        )
        if "cover" in payload or "images" in payload:
            update_assets(conn, archive_id, payload.get("cover"), payload.get("images") or [], commit=False)
            conn.commit()
        else:
            conn.commit()
        row = get_archive_or_404(conn, archive_id)
        return jsonify({"item": archive_summary(row, conn)})


@app.post("/api/archives/<archive_id>/favorite")
def api_toggle_favorite(archive_id: str):
    payload = request.get_json(force=True, silent=True) or {}
    favorite = payload.get("favorite")
    with connect_db() as conn:
        row = get_archive_or_404(conn, archive_id)
        value = (not bool(row["favorite"])) if favorite is None else bool(favorite)
        conn.execute("UPDATE archives SET favorite = ?, updated_at = ? WHERE id = ?", (1 if value else 0, now_iso(), archive_id))
        conn.commit()
        row = get_archive_or_404(conn, archive_id)
        return jsonify({"item": archive_summary(row, conn)})


@app.put("/api/archives/<archive_id>/feedback")
def api_put_feedback(archive_id: str):
    blocked = legacy_write_guard()
    if blocked:
        return blocked
    payload = request.get_json(force=True, silent=False) or {}
    platform = (payload.get("platform") or "").strip()
    if platform not in PLATFORMS:
        return jsonify({"error": f"unsupported platform: {platform}"}), 400
    metrics = payload.get("metrics") or payload
    with connect_db() as conn:
        get_archive_or_404(conn, archive_id)
        upsert_feedback(conn, archive_id, platform, metrics, source=payload.get("source", "manual"))
        row = get_archive_or_404(conn, archive_id)
        return jsonify({"item": archive_summary(row, conn)})


@app.put("/api/archives/<archive_id>/link")
def api_put_archive_link(archive_id: str):
    payload = request.get_json(force=True, silent=False) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    platform_hint = (payload.get("platform") or "").strip()
    with connect_db() as conn:
        get_archive_or_404(conn, archive_id)
        snapshot = capture_link_snapshot(url, platform_hint, request.headers.get("X-Request-ID", "").strip())
        if not snapshot.get("platform"):
            snapshot["platform"] = platform_hint or detect_platform_from_url(url)
        if snapshot.get("status") == "fetched" and snapshot.get("platform") in PLATFORMS:
            link_metrics = dict(snapshot.get("metrics") or {})
            link_metrics["account"] = snapshot.get("author") or snapshot.get("site_name") or platform_hint or ""
            upsert_feedback(
                conn,
                archive_id,
                snapshot["platform"],
                link_metrics,
                source="link_capture",
                commit=False,
            )
        conn.execute(
            "UPDATE archives SET published_url = ?, published_snapshot_json = ?, updated_at = ? WHERE id = ?",
            (url, json.dumps(snapshot, ensure_ascii=False), now_iso(), archive_id),
        )
        conn.commit()
        row = get_archive_or_404(conn, archive_id)
        return jsonify({"item": archive_summary(row, conn)})


@app.post("/api/ingest")
def api_ingest_batch():
    blocked = legacy_write_guard()
    if blocked:
        return blocked
    payload = request.get_json(force=True, silent=False) or {}
    items = payload.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    created = []
    with connect_db() as conn:
        for item in items:
            if not isinstance(item, dict):
                continue
            archive_id = create_archive(conn, item, commit=False)
            update_assets(conn, archive_id, item.get("cover"), item.get("images") or [], commit=False)
            for platform, metrics in (item.get("feedback") or {}).items():
                if platform in PLATFORMS:
                    upsert_feedback(conn, archive_id, platform, metrics or {}, source=item.get("source", "cli"), commit=False)
            row = get_archive_or_404(conn, archive_id)
            created.append(archive_summary(row, conn))
        conn.commit()
    return jsonify({"created": created})


@app.delete("/api/archives/<archive_id>")
def api_delete_archive(archive_id: str):
    with connect_db() as conn:
        get_archive_or_404(conn, archive_id)
        conn.execute("DELETE FROM metric_history WHERE archive_id = ?", (archive_id,))
        conn.execute("DELETE FROM feedback WHERE archive_id = ?", (archive_id,))
        conn.execute("DELETE FROM assets WHERE archive_id = ?", (archive_id,))
        conn.execute("DELETE FROM archives WHERE id = ?", (archive_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/archives/<archive_id>/asset/<kind>/<int:index>")
def api_delete_asset(archive_id: str, kind: str, index: int):
    if kind not in ASSET_KINDS:
        return jsonify({"error": f"unsupported asset kind: {kind}"}), 400
    with connect_db() as conn:
        get_archive_or_404(conn, archive_id)
        delete_asset(conn, archive_id, kind, index)
        row = get_archive_or_404(conn, archive_id)
        return jsonify({"item": archive_summary(row, conn)})


@app.after_request
def add_security_headers(resp):
    if PANEL_ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = PANEL_ALLOWED_ORIGIN
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Request-ID"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,PUT,DELETE,OPTIONS"
        resp.headers["Vary"] = "Origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(err):
    return jsonify({"error": getattr(err, "description", "not found")}), 404


@app.errorhandler(400)
def bad_request(err):
    return jsonify({"error": getattr(err, "description", "bad request")}), 400


if __name__ == "__main__":
    init_db()
    seed_accounts_from_feedback()
    from ai_service import start_worker
    start_worker(connect_db)
    app.run(host="127.0.0.1", port=int(os.getenv("PANEL_PORT", "5060")), debug=False)
