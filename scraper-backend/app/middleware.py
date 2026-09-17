"""HTTP 中间件：X-Request-ID 透传 + X-API-Key 鉴权。"""

from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .errors import ErrorCode

logger = logging.getLogger("scraper.middleware")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求注入 X-Request-ID（如果上游没传），并回传到响应头。"""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


class APIKeyMiddleware(BaseHTTPMiddleware):
    """如果 SCRAPER_API_KEY 已配置，所有 /api/* HTTP 请求必须带 X-API-Key。

    WebSocket 路径（/api/ws/bridge）放行，让浏览器扩展无密钥连上。
    """

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if not self.api_key:
            return await call_next(request)
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        # WS 桥接路径放行
        if request.url.path.startswith("/api/ws/"):
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if provided != self.api_key:
            message = "Invalid or missing API key"
            rid = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")
            error = {
                "code": ErrorCode.SERVICE_UNAUTHORIZED,
                "message": message,
                "retryable": False,
            }
            if rid:
                error["request_id"] = rid
            return JSONResponse(
                status_code=401,
                content={"detail": message, "error": error},
            )
        return await call_next(request)
