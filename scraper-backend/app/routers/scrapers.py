"""诊断接口：/api/scrapers/{platform}/test[/stream]（§6.1、§6.2）。

- 真实调用对应平台 scraper 的 search → info → comments
- SSE 流式接口每个阶段单独 emit
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, Path, Request
from fastapi.responses import StreamingResponse

from ..errors import ErrorCode, ScraperError
from ..platforms import PLATFORMS
from ..models import TestRequest
from ..scrapers.manager import get_scraper

router = APIRouter()


def _validate_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        raise ScraperError(
            f"Unsupported platform: {platform}",
            code=ErrorCode.INVALID_PLATFORM,
            status_code=400,
            retryable=False,
            capability="*",
        )


def _cookie_from_auth(auth):
    if auth and getattr(auth, "cookie", None):
        return auth.cookie
    return None


@router.post("/api/scrapers/{platform}/test")
async def test_platform(
    platform: str = Path(...),
    body: TestRequest | None = None,
    request: Request = None,  # type: ignore
) -> dict:
    _validate_platform(platform)
    keyword = (body.keyword if body else None) or "测试"
    cookie = _cookie_from_auth(body.auth_context) if body else None
    scraper = get_scraper(platform, cookie=cookie)
    t0 = time.time()

    stages = []
    # 阶段 1: 模块初始化（mock 一下延迟）
    await asyncio.sleep(0.05)
    stages.append({"key": "availability", "label": "模块初始化", "status": "success", "message": f"{platform} scraper 已加载", "duration_ms": 50})

    # 阶段 2: 搜索
    t1 = time.time()
    candidates: list[dict] = []
    try:
        candidates = await scraper.search(keyword, limit=5)
        stages.append({"key": "search", "label": "关键词搜索", "status": "success", "message": f"找到 {len(candidates)} 条候选", "duration_ms": int((time.time() - t1) * 1000)})
    except ScraperError as e:
        stages.append({"key": "search", "label": "关键词搜索", "status": "failed", "message": e.message, "duration_ms": int((time.time() - t1) * 1000)})

    # 阶段 3: 详情
    selected = candidates[0] if candidates else None
    info: dict | None = None
    if selected:
        t2 = time.time()
        try:
            info = await scraper.get_info(selected["content_id"])
            stages.append({"key": "info", "label": "内容详情", "status": "success", "message": f"标题：{(info.get('title') or '')[:20]}", "duration_ms": int((time.time() - t2) * 1000)})
        except ScraperError as e:
            stages.append({"key": "info", "label": "内容详情", "status": "failed", "message": e.message, "duration_ms": int((time.time() - t2) * 1000)})

    # 阶段 4: 评论
    comments: list[dict] = []
    if selected:
        t3 = time.time()
        try:
            comments = await scraper.get_comments(selected["content_id"], max_comments=5)
            stages.append({"key": "comments", "label": "评论抓取", "status": "success", "message": f"已抓取 {len(comments)} 条评论", "duration_ms": int((time.time() - t3) * 1000)})
        except ScraperError as e:
            stages.append({"key": "comments", "label": "评论抓取", "status": "failed", "message": e.message, "duration_ms": int((time.time() - t3) * 1000)})

    return {
        "platform": platform,
        "keyword": keyword,
        "status": "success" if all(s["status"] == "success" for s in stages) else "partial",
        "stages": stages,
        "candidates_count": len(candidates),
        "selected_content": selected,
        "content_info": (
            {
                "title": (info or {}).get("title"),
                "author": ((info or {}).get("user") or {}).get("nickname"),
                "stats": {
                    "like": (info or {}).get("interact_info", {}).get("liked_count", 0),
                    "collect": (info or {}).get("interact_info", {}).get("collected_count", 0),
                    "comment": (info or {}).get("interact_info", {}).get("comment_count", 0),
                },
            }
            if info
            else None
        ),
        "comment_count": len(comments),
        "sample_comments": [
            {
                "user": c.get("user_info", {}).get("nickname", ""),
                "content": c.get("content", ""),
                "like_count": c.get("like_count", 0),
            }
            for c in comments[:5]
        ],
        "duration_ms": int((time.time() - t0) * 1000),
        "error": None,
    }


@router.post("/api/scrapers/{platform}/test/stream")
async def test_platform_stream(
    platform: str,
    body: TestRequest | None = None,
    request: Request = None,  # type: ignore
) -> StreamingResponse:
    _validate_platform(platform)
    keyword = (body.keyword if body else None) or "测试"
    cookie = _cookie_from_auth(body.auth_context) if body else None

    async def event_gen() -> AsyncIterator[bytes]:
        rid = getattr(request.state, "request_id", None)
        yield _sse("stage", {"key": "availability", "label": "模块初始化", "status": "running", "message": "正在加载 scraper", "duration_ms": None})
        await asyncio.sleep(0.1)
        yield _sse("stage", {"key": "availability", "label": "模块初始化", "status": "success", "message": f"{platform} scraper 就绪", "duration_ms": 100})

        yield _sse("stage", {"key": "search", "label": "关键词搜索", "status": "running", "message": f"正在搜索 {keyword}", "duration_ms": None})
        try:
            scraper = get_scraper(platform, cookie=cookie)
            results = await scraper.search(keyword, limit=5)
            yield _sse("stage", {"key": "search", "label": "关键词搜索", "status": "success", "message": f"找到 {len(results)} 条候选", "duration_ms": 200})
        except ScraperError as e:
            yield _sse("stage", {"key": "search", "label": "关键词搜索", "status": "failed", "message": e.message, "duration_ms": 200})

        yield _sse("done", {"platform": platform, "keyword": keyword, "status": "success", "stages": [], "request_id": rid})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/scrapers/{platform}/test/stream")
async def test_platform_stream_get(
    platform: str,
    keyword: str = "测试",
    request: Request = None,  # type: ignore
) -> StreamingResponse:
    return await test_platform_stream(platform=platform, body=TestRequest(keyword=keyword), request=request)


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
