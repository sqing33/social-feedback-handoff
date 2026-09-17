"""Pydantic 数据模型，对应 scraper-service-api.md §3 通用数据结构。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AuthContext(BaseModel):
    account_id: Optional[int] = None
    name: Optional[str] = None
    cookie: Optional[str] = None


class ProxyContext(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    scheme: str = "http"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    url: Optional[str] = None


class UserInfo(BaseModel):
    nickname: str = ""
    user_id: Optional[str] = None
    profile_url: Optional[str] = None


class InteractInfo(BaseModel):
    # None means the page/state did not expose the metric. An explicit 0 means
    # the scraper found the metric and the real value was zero.
    view_count: Optional[int] = None
    play_count: Optional[int] = None
    liked_count: Optional[int] = None
    collected_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None


class NoteInfo(BaseModel):
    content_id: str = ""
    title: str = ""
    description: str = ""
    user: UserInfo = Field(default_factory=UserInfo)
    cover: str = ""
    images: list[str] = Field(default_factory=list)
    url: str = ""
    publish_time: Optional[int] = None
    interact_info: InteractInfo = Field(default_factory=InteractInfo)
    metric_presence: dict[str, bool] = Field(default_factory=dict)


class SubComment(BaseModel):
    user_info: UserInfo = Field(default_factory=UserInfo)
    content: str = ""
    like_count: int = 0
    create_time: int = 0


class UnifiedComment(BaseModel):
    user_info: UserInfo = Field(default_factory=UserInfo)
    content: str = ""
    like_count: Optional[int] = None
    create_time: Optional[int] = None
    create_time_text: str = ""
    sub_comments: list[SubComment] = Field(default_factory=list)


class SearchStats(BaseModel):
    view: Optional[int] = None
    like: Optional[int] = None
    collect: Optional[int] = None
    reply: Optional[int] = None
    share: Optional[int] = None


class SearchResult(BaseModel):
    content_id: str
    title: str = ""
    author: str = ""
    url: str = ""
    stats: SearchStats = Field(default_factory=SearchStats)
    cover: str = ""
    author_avatar: str = ""
    duration_ms: Optional[int] = None
    tags: list[str] = Field(default_factory=list)
    xsec_token: str = ""
    xsec_source: str = ""
    discovery_strength: str = ""
    profile_url: str = ""
    user_id: Optional[str] = None
    # 平台原生 ID（兼容字段）
    bvid: Optional[str] = None
    note_id: Optional[str] = None
    tweet_id: Optional[str] = None
    video_id: Optional[str] = None
    aweme_id: Optional[str] = None
    photo_id: Optional[str] = None


class MetaInfo(BaseModel):
    request_id: Optional[str] = None
    backend: str = "unknown"
    degraded: bool = False
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    identity_outcome: Optional[dict[str, Any]] = None


# ----- 请求体 -----


class CommentsRequest(BaseModel):
    platform: str
    content_id: str
    xsec_token: Optional[str] = None
    xsec_source: Optional[str] = None
    extra: Optional[str] = None
    kwargs: Optional[dict[str, Any]] = None
    auth_context: Optional[AuthContext] = None
    proxy_context: Optional[ProxyContext] = None


class CommentsResponse(BaseModel):
    comments: list[UnifiedComment]
    note_info: Optional[NoteInfo] = None
    meta: Optional[MetaInfo] = None


class InfoRequest(BaseModel):
    platform: str
    content_id: str
    extra: Optional[str] = None
    kwargs: Optional[dict[str, Any]] = None
    auth_context: Optional[AuthContext] = None
    proxy_context: Optional[ProxyContext] = None


class InfoResponse(BaseModel):
    note_info: Optional[NoteInfo] = None
    meta: Optional[MetaInfo] = None


class SearchRequest(BaseModel):
    platform: str
    keyword: str
    limit: int = 20
    kwargs: Optional[dict[str, Any]] = None
    auth_context: Optional[AuthContext] = None
    proxy_context: Optional[ProxyContext] = None


class SearchResponse(BaseModel):
    results: list[SearchResult]
    meta: Optional[MetaInfo] = None


class CallRequest(BaseModel):
    platform: str
    method: str
    args: list[Any] = Field(default_factory=list)
    kwargs: Optional[dict[str, Any]] = None
    auth_context: Optional[AuthContext] = None
    proxy_context: Optional[ProxyContext] = None


class CallResponse(BaseModel):
    result: Any
    meta: Optional[MetaInfo] = None


class TestRequest(BaseModel):
    keyword: str = "测试"
    auth_context: Optional[AuthContext] = None
    proxy_context: Optional[ProxyContext] = None


class ProxyTestRequest(BaseModel):
    proxy_context: ProxyContext


class PlatformTestStage(BaseModel):
    key: str
    label: str
    status: str
    message: str = ""
    duration_ms: int = 0
