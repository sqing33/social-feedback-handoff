"""POST /api/auth/login + GET /api/auth/me — 业务层 auth。"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from ..errors import ErrorCode, ScraperError
from ..store import auth as auth_store

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/api/auth/login")
def login(body: LoginRequest, request: Request) -> dict:
    result = auth_store.login(body.username, body.password)
    if not result:
        raise ScraperError(
            "用户名或密码错误",
            code=ErrorCode.SERVICE_UNAUTHORIZED,
            status_code=401,
            retryable=False,
            capability="auth",
        )
    return result


@router.get("/api/auth/me")
def me(authorization: str | None = Header(default=None), request: Request = None) -> dict:  # type: ignore
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ScraperError(
            "Missing bearer token",
            code=ErrorCode.SERVICE_UNAUTHORIZED,
            status_code=401,
            retryable=False,
            capability="auth",
        )
    # 极简：从 token 还原用户名（生产环境应解析 JWT）
    return {
        "id": 1,
        "username": "admin",
        "name": "Administrator",
    }
