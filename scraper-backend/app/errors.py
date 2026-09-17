"""统一错误响应。

所有业务错误 MUST 使用以下结构（scraper-service-api.md §2.5）：

{
  "detail": "human readable message",
  "error": {
    "code": "SCRAPE_ERROR",
    "message": "human readable message",
    "retryable": true,
    "retry_after_seconds": 30,
    "backend": "...",
    "capability": "...",
    "request_id": "..."
  }
}
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode:
    INVALID_PLATFORM = "INVALID_PLATFORM"
    INVALID_CONTENT_ID = "INVALID_CONTENT_ID"
    CONTENT_NOT_FOUND = "CONTENT_NOT_FOUND"
    PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PROXY_ERROR = "PROXY_ERROR"
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    SCRAPE_ERROR = "SCRAPE_ERROR"
    SERVICE_UNAUTHORIZED = "SERVICE_UNAUTHORIZED"


# 错误码 -> 推荐 HTTP 状态码
DEFAULT_HTTP_STATUS: dict[str, int] = {
    ErrorCode.INVALID_PLATFORM: 400,
    ErrorCode.INVALID_CONTENT_ID: 400,
    ErrorCode.CONTENT_NOT_FOUND: 404,
    ErrorCode.PLATFORM_UNAVAILABLE: 503,
    ErrorCode.CREDENTIAL_EXPIRED: 401,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PROXY_ERROR: 502,
    ErrorCode.SIGNATURE_INVALID: 502,
    ErrorCode.BACKEND_UNAVAILABLE: 503,
    ErrorCode.CAPACITY_EXCEEDED: 503,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.SCRAPE_ERROR: 500,
    ErrorCode.SERVICE_UNAUTHORIZED: 401,
}


class ScraperError(Exception):
    """所有业务异常的基类。"""

    code: str = ErrorCode.SCRAPE_ERROR
    status_code: int = 500
    retryable: bool = True
    backend: Optional[str] = None
    capability: Optional[str] = None

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        retryable: Optional[bool] = None,
        retry_after_seconds: Optional[int] = None,
        backend: Optional[str] = None,
        capability: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        if retry_after_seconds is not None:
            self.retry_after_seconds = retry_after_seconds
        if backend is not None:
            self.backend = backend
        if capability is not None:
            self.capability = capability


def _build_error_body(
    *,
    code: str,
    message: str,
    request_id: Optional[str],
    retryable: bool = True,
    retry_after_seconds: Optional[int] = None,
    backend: Optional[str] = None,
    capability: Optional[str] = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
    }
    if retry_after_seconds is not None:
        err["retry_after_seconds"] = retry_after_seconds
    if backend is not None:
        err["backend"] = backend
    if capability is not None:
        err["capability"] = capability
    if request_id is not None:
        err["request_id"] = request_id
    return {"detail": message, "error": err}


def _get_request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")


async def scraper_exception_handler(request: Request, exc: ScraperError) -> JSONResponse:
    body = _build_error_body(
        code=exc.code,
        message=exc.message,
        request_id=_get_request_id(request),
        retryable=exc.retryable,
        retry_after_seconds=getattr(exc, "retry_after_seconds", None),
        backend=exc.backend,
        capability=exc.capability,
    )
    return JSONResponse(status_code=exc.status_code, content=body)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = ErrorCode.SCRAPE_ERROR
    if exc.status_code == 401:
        code = ErrorCode.SERVICE_UNAUTHORIZED
    elif exc.status_code == 404:
        code = ErrorCode.CONTENT_NOT_FOUND
    elif exc.status_code == 405:
        return JSONResponse(status_code=405, content={"detail": exc.detail})
    body = _build_error_body(
        code=code,
        message=str(exc.detail) if exc.detail else "HTTP error",
        request_id=_get_request_id(request),
        retryable=exc.status_code >= 500,
    )
    return JSONResponse(status_code=exc.status_code, content=body)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    body = _build_error_body(
        code=ErrorCode.INVALID_CONTENT_ID,
        message=f"Validation error: {exc.errors()[0]['msg'] if exc.errors() else 'invalid request'}",
        request_id=_get_request_id(request),
        retryable=False,
    )
    return JSONResponse(status_code=400, content=body)
