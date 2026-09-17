"""核心抓取接口：/api/scrape/comments | info | search | call。

- 实际抓取走 app.scrapers.manager
- 失败时把异常向上抛，HTTP 中间件统一格式化
- meta.backend / meta.degraded 透传抓取器状态
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request

from ..errors import ErrorCode, ScraperError
from ..platforms import PLATFORMS
from ..scrapers.manager import get_scraper
from ..models import (
    CallRequest,
    CallResponse,
    CommentsRequest,
    CommentsResponse,
    InfoRequest,
    InfoResponse,
    MetaInfo,
    SearchRequest,
    SearchResponse,
)

logger = logging.getLogger("scraper.router")
router = APIRouter()

ALLOWED_METHODS = {
    "get_comments",
    "get_note_info",
    "get_video_info",
    "get_video_comments",
    "search_videos",
    "search_videos_extended",
    "search_notes",
    "search_tweets",
    "get_user_videos",
}


def _validate_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        raise ScraperError(
            f"Unsupported platform: {platform}",
            code=ErrorCode.INVALID_PLATFORM,
            status_code=400,
            retryable=False,
            capability="*",
        )


def _validate_content_id(content_id: str, platform: str) -> None:
    if not content_id or not content_id.strip() or len(content_id.strip()) < 2:
        raise ScraperError(
            f"Invalid content id for {platform}: {content_id!r}",
            code=ErrorCode.INVALID_CONTENT_ID,
            status_code=400,
            retryable=False,
            capability="*",
        )


def _cookie_from_auth(auth: Any) -> str | None:
    if auth and getattr(auth, "cookie", None):
        return auth.cookie
    return None


def _proxy_url(proxy: Any) -> str | None:
    if proxy and getattr(proxy, "url", None):
        return proxy.url
    return None


# ---------- 4.1 comments ----------
@router.post("/api/scrape/comments", response_model=CommentsResponse)
async def scrape_comments(body: CommentsRequest, request: Request) -> CommentsResponse:
    _validate_platform(body.platform)
    _validate_content_id(body.content_id, body.platform)
    scraper = get_scraper(body.platform, cookie=_cookie_from_auth(body.auth_context), proxy=_proxy_url(body.proxy_context))
    kwargs = dict(body.kwargs or {})
    max_comments = int(kwargs.pop("max_comments", 20))
    include_info = bool(kwargs.pop("include_info", True))
    content_identifier = body.content_id
    if body.platform == "xhs":
        if body.extra and "xsec_token=" in body.extra:
            content_identifier = body.extra
        elif body.xsec_token and "xsec_token=" not in content_identifier:
            params = [("xsec_token", body.xsec_token)]
            if body.xsec_source:
                params.append(("xsec_source", body.xsec_source))
            content_identifier = f"{content_identifier}{'&' if '?' in content_identifier else '?'}{urlencode(params)}"
    t0 = time.time()
    try:
        comments = await scraper.get_comments(content_identifier, max_comments=max_comments, **kwargs)
    except ScraperError:
        raise
    except Exception as e:
        logger.exception("bilibili comments unexpected")
        raise ScraperError(
            f"scraper raised: {e}",
            code=ErrorCode.SCRAPE_ERROR,
            status_code=500,
            retryable=True,
            capability="comments",
        )
    # 调用方需要时才补充 info；面板已先取详情，可关闭以避免重复导航触发平台风控。
    info = None
    if include_info:
        try:
            info = await scraper.get_info(content_identifier)
        except ScraperError as e:
            # info 失败不回滚整体请求
            logger.info("info failed for %s/%s: %s", body.platform, body.content_id, e.message)

    rid = getattr(request.state, "request_id", None)
    return CommentsResponse(
        comments=comments,
        note_info=info,
        meta=MetaInfo(
            request_id=rid,
            backend=f"{body.platform}-real",
            degraded=False,
            attempts=[{
                "backend": f"{body.platform}-real",
                "status": "success",
                "used_identity": body.auth_context is not None,
            }],
            identity_outcome={"status": "success" if body.auth_context else "anonymous"},
        ),
    )


# ---------- 4.2 info ----------
@router.post("/api/scrape/info", response_model=InfoResponse)
async def scrape_info(body: InfoRequest, request: Request) -> InfoResponse:
    _validate_platform(body.platform)
    _validate_content_id(body.content_id, body.platform)
    scraper = get_scraper(body.platform, cookie=_cookie_from_auth(body.auth_context), proxy=_proxy_url(body.proxy_context))
    if body.platform == "xhs":
        info = await scraper.get_info(
            body.content_id,
            account_import=bool((body.kwargs or {}).get("account_import", False)),
        )
    else:
        info = await scraper.get_info(body.content_id)
    rid = getattr(request.state, "request_id", None)
    return InfoResponse(
        note_info=info,
        meta=MetaInfo(
            request_id=rid,
            backend=f"{body.platform}-real",
            degraded=False,
            attempts=[{
                "backend": f"{body.platform}-real",
                "status": "success",
            }],
        ),
    )


# ---------- 4.3 search ----------
@router.post("/api/scrape/search", response_model=SearchResponse)
async def scrape_search(body: SearchRequest, request: Request) -> SearchResponse:
    _validate_platform(body.platform)
    if not body.keyword or not body.keyword.strip():
        raise ScraperError(
            "Empty keyword",
            code=ErrorCode.INVALID_CONTENT_ID,
            status_code=400,
            retryable=False,
            capability="search",
        )
    scraper = get_scraper(body.platform, cookie=_cookie_from_auth(body.auth_context), proxy=_proxy_url(body.proxy_context))
    try:
        results = await scraper.search(body.keyword, limit=body.limit)
    except ScraperError:
        raise
    except Exception:
        results = []
    rid = getattr(request.state, "request_id", None)
    return SearchResponse(
        results=results,
        meta=MetaInfo(
            request_id=rid,
            backend=f"{body.platform}-real",
            degraded=len(results) == 0,
            attempts=[{"backend": f"{body.platform}-real", "status": "partial" if not results else "success"}],
        ),
    )


# ---------- 4.4 call ----------
@router.post("/api/scrape/call", response_model=CallResponse)
async def scrape_call(body: CallRequest, request: Request) -> CallResponse:
    _validate_platform(body.platform)
    if body.method not in ALLOWED_METHODS:
        raise ScraperError(
            f"Unknown method: {body.method}",
            code=ErrorCode.SCRAPE_ERROR,
            status_code=400,
            retryable=False,
            capability="call",
        )
    scraper = get_scraper(body.platform, cookie=_cookie_from_auth(body.auth_context), proxy=_proxy_url(body.proxy_context))
    result: Any
    if body.method in ("search_videos", "search_videos_extended", "search_notes", "search_tweets"):
        keyword = body.args[0] if body.args else (body.kwargs or {}).get("keyword", "")
        limit = int((body.kwargs or {}).get("page_size", 20))
        result = await scraper.search(str(keyword), limit=limit)
    elif body.method == "get_user_videos":
        kwargs = body.kwargs or {}
        identifier = body.args[0] if body.args else (
            kwargs.get("profile_url") or kwargs.get("identifier") or kwargs.get("uid") or ""
        )
        account_name = str(kwargs.get("account_name") or "")
        raw_limit = next(
            (kwargs[key] for key in ("max_results", "page_size", "limit") if kwargs.get(key) is not None),
            20,
        )
        result = await scraper.get_user_contents(
            str(identifier or ""),
            account_name=account_name,
            limit=max(0, int(raw_limit)),
        )
    elif body.method in ("get_comments", "get_video_comments"):
        cid = body.args[0] if body.args else (body.kwargs or {}).get("content_id", "")
        result = await scraper.get_comments(str(cid))
    elif body.method in ("get_note_info", "get_video_info"):
        cid = body.args[0] if body.args else (body.kwargs or {}).get("content_id", "")
        result = await scraper.get_info(str(cid))
    else:
        result = []

    rid = getattr(request.state, "request_id", None)
    return CallResponse(
        result=result,
        meta=MetaInfo(
            request_id=rid,
            backend=f"{body.platform}-real",
            degraded=False,
            attempts=[{"backend": f"{body.platform}-real", "status": "success"}],
        ),
    )
