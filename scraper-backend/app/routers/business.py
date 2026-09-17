"""业务层兼容接口：把前端调用的 12 个业务模块都接上后端。"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..store import (
    accounts as accounts_store,
    agent_tasks as agent_store,
    comments as comments_store,
    dashboard as dashboard_store,
    events as events_store,
    insights as insights_store,
    lexicon as lexicon_store,
    library as library_store,
    proxies as proxies_store,
    scheduled as scheduled_store,
    settings as settings_store,
    teams as teams_store,
)

router = APIRouter()


# ---------- teams ----------
@router.get("/api/teams")
def list_teams(request: Request) -> dict:
    return {"total": len(teams_store.list()), "items": teams_store.list()}


# ---------- events ----------
@router.get("/api/intelligence/events")
def list_events(
    risk: Optional[str] = None,
    status: Optional[str] = None,
    request: Request = None,  # type: ignore
) -> dict:
    return events_store.list(risk=risk, status=status)


@router.get("/api/intelligence/events/{id}")
def event_detail(id: str, request: Request = None) -> dict:  # type: ignore
    return events_store.detail(id)


# ---------- dashboard ----------
@router.get("/api/dashboard/metrics")
def dashboard_metrics(request: Request = None) -> dict:  # type: ignore
    return dashboard_store.metrics()


# ---------- comments ----------
@router.get("/api/comments")
def list_comments(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    request: Request = None,  # type: ignore
) -> dict:
    return comments_store.list(platform=platform, status=status)


class ReplyBody(BaseModel):
    text: str


@router.post("/api/comments/{id}/reply")
def reply_comment(id: str, body: ReplyBody, request: Request = None) -> dict:  # type: ignore
    return comments_store.reply(id, body.text)


@router.post("/api/comments/{id}/hide")
def hide_comment(id: str, request: Request = None) -> dict:  # type: ignore
    return comments_store.hide(id)


# ---------- library ----------
@router.get("/api/library")
def list_library(
    q: Optional[str] = None,
    platform: Optional[str] = None,
    request: Request = None,  # type: ignore
) -> dict:
    return library_store.list(q=q, platform=platform)


# ---------- insights ----------
@router.get("/api/insights")
def get_insights(request: Request = None) -> dict:  # type: ignore
    return insights_store.all()


# ---------- discover ----------
@router.get("/api/discover")
def discover(
    q: str = Query(...),
    platform: Optional[str] = None,
    request: Request = None,  # type: ignore
) -> dict:
    base = [
        {"platform": "小红书", "title": f"{q} 种草合集", "author": "种草机", "likes": 2300, "comments": 120},
        {"platform": "B站", "title": f"{q} 深度评测", "author": "硬件菌", "likes": 12_400, "comments": 880},
        {"platform": "抖音", "title": f"{q} 开箱", "author": "日常打卡", "likes": 8200, "comments": 320},
        {"platform": "微博", "title": f"{q} 话题讨论", "author": "话题速报", "likes": 18_000, "comments": 2_100},
    ]
    items = (
        [b for b in base if b["platform"] == platform]
        if platform and platform != "all"
        else base
    )
    return {"q": q, "total": len(items), "items": items}


# ---------- hotspots ----------
_HOTSPOTS_BY_TRACK: dict[str, list[dict]] = {
    "美妆": [
        {"keyword": "早八妆容", "heat": 92, "delta": 18},
        {"keyword": "平价彩妆", "heat": 81, "delta": 4},
        {"keyword": "敏感肌", "heat": 73, "delta": 12},
    ],
    "3C": [
        {"keyword": "折叠屏", "heat": 88, "delta": 7},
        {"keyword": "国产芯片", "heat": 79, "delta": 15},
        {"keyword": "AI PC", "heat": 71, "delta": 22},
    ],
    "汽车": [
        {"keyword": "新能源 SUV", "heat": 86, "delta": 9},
        {"keyword": "智驾系统", "heat": 78, "delta": 13},
    ],
    "母婴": [
        {"keyword": "新生儿睡眠", "heat": 70, "delta": 5},
        {"keyword": "辅食机", "heat": 64, "delta": 2},
    ],
    "default": [
        {"keyword": "星野", "heat": 94, "delta": 22},
        {"keyword": "主播跳槽", "heat": 78, "delta": -3},
        {"keyword": "行业安全标准", "heat": 66, "delta": 4},
    ],
}


@router.get("/api/hotspots")
def list_hotspots(
    track: Optional[str] = None,
    request: Request = None,  # type: ignore
) -> dict:
    items = _HOTSPOTS_BY_TRACK.get(track or "default", _HOTSPOTS_BY_TRACK["default"])
    return {"track": track or "default", "items": items}


# ---------- lexicon ----------
@router.get("/api/lexicon")
def list_lexicon(request: Request = None) -> dict:  # type: ignore
    items = lexicon_store.list()
    return {"total": len(items), "items": items}


class LexiconBody(BaseModel):
    term: str
    kind: str
    weight: Optional[float] = None


@router.post("/api/lexicon")
def create_lexicon(body: LexiconBody, request: Request = None) -> dict:  # type: ignore
    return lexicon_store.create(body.model_dump(exclude_none=True))


@router.delete("/api/lexicon/{id}")
def delete_lexicon(id: str, request: Request = None) -> dict:  # type: ignore
    return lexicon_store.delete(id)


# ---------- scheduled-tasks ----------
@router.get("/api/scheduled-tasks")
def list_scheduled(request: Request = None) -> dict:  # type: ignore
    items = scheduled_store.list()
    return {"total": len(items), "items": items}


class ToggleBody(BaseModel):
    enabled: bool


@router.post("/api/scheduled-tasks/{id}/toggle")
def toggle_scheduled(id: str, body: ToggleBody, request: Request = None) -> dict:  # type: ignore
    return scheduled_store.toggle(id, body.enabled)


# ---------- agent ----------
@router.get("/api/agent/tasks")
def list_agent_tasks(request: Request = None) -> dict:  # type: ignore
    items = agent_store.list()
    return {"total": len(items), "items": items}


# ---------- accounts ----------
@router.get("/api/accounts")
def list_accounts(request: Request = None) -> dict:  # type: ignore
    return {
        "accounts": accounts_store.list(),
        "proxies": proxies_store.list(),
    }


class AccountBody(BaseModel):
    platform: str
    name: str
    status: str = "healthy"
    last_used: Optional[int] = None
    requests_today: Optional[int] = None


@router.post("/api/accounts")
def create_account(body: AccountBody, request: Request = None) -> dict:  # type: ignore
    return accounts_store.create(body.model_dump(exclude_none=True))


@router.put("/api/accounts/{id}")
def update_account(
    id: str,
    body: dict[str, Any] = None,
    request: Request = None,  # type: ignore
) -> dict:
    return accounts_store.update(id, body or {})


@router.delete("/api/accounts/{id}")
def delete_account(id: str, request: Request = None) -> dict:  # type: ignore
    return accounts_store.delete(id)


# ---------- proxies ----------
class ProxyBody(BaseModel):
    name: str
    scheme: str = "http"
    host: str
    port: int
    status: str = "healthy"
    latency_ms: Optional[int] = None


@router.post("/api/proxies")
def create_proxy(body: ProxyBody, request: Request = None) -> dict:  # type: ignore
    return proxies_store.create(body.model_dump(exclude_none=True))


@router.put("/api/proxies/{id}")
def update_proxy(
    id: str,
    body: dict[str, Any] = None,
    request: Request = None,  # type: ignore
) -> dict:
    return proxies_store.update(id, body or {})


@router.delete("/api/proxies/{id}")
def delete_proxy(id: str, request: Request = None) -> dict:  # type: ignore
    return proxies_store.delete(id)


# ---------- settings ----------
@router.get("/api/settings")
def get_settings(request: Request = None) -> dict:  # type: ignore
    return settings_store.get()


@router.put("/api/settings")
def save_settings(body: dict[str, Any], request: Request = None) -> dict:  # type: ignore
    return settings_store.save(body)
