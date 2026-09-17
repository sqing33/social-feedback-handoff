"""业务层数据 + CRUD 方法。

为前端 sonar-frontend 提供兼容的接口数据格式（和前端 mock.ts 一致）。
所有 store 都在模块级维护内存数据，进程重启即重置。
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Any, Optional

# ---------- helpers ----------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _ago_ms(hours: float) -> int:
    return int((time.time() - hours * 3600) * 1000)


def _seed(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=False)


def _next_id(items: list) -> int:
    return (max((int(x.get("id", 0)) for x in items), default=0) + 1)


# ---------- Auth ----------

class AuthStore:
    """极简内存版 auth：admin/admin123 默认通过。"""

    def login(self, username: str, password: str) -> Optional[dict]:
        if username == "admin" and password == "admin123":
            return {
                "token": "mock-token-" + hashlib.md5(f"{username}:{time.time()}".encode()).hexdigest()[:16],
                "user": {"id": 1, "username": "admin", "name": "Administrator"},
            }
        return None


auth = AuthStore()


# ---------- Teams ----------

_TEAMS = [
    {"id": 1, "name": "所有团队"},
    {"id": 2, "name": "品牌一组"},
    {"id": 3, "name": "品牌二组"},
    {"id": 4, "name": "公关应急"},
]

class TeamsStore:
    def list(self) -> list[dict]:
        return list(_TEAMS)


teams = TeamsStore()


# ---------- Events ----------

_EVENTS = [
    {
        "id": 1, "title": "现在的星野还能玩吗？",
        "excerpt": "多个玩家反馈登录异常、匹配延迟上升，客服响应变慢。",
        "risk": "high", "status": "rising", "keyword": "星野",
        "platforms": 3, "evidence": 28, "interactions": 13001, "trend": 84,
        "created_at": _ago_ms(2),
    },
    {
        "id": 2, "title": "某品牌新品发布褒贬不一",
        "excerpt": "功能好评、外观争议、定价分歧；竞品借势对比热度上升。",
        "risk": "mid", "status": "stable", "keyword": "新品发布",
        "platforms": 5, "evidence": 64, "interactions": 56200, "trend": 12,
        "created_at": _ago_ms(8),
    },
    {
        "id": 3, "title": "星野要跑路了！家人们快…",
        "excerpt": "传闻扩散，伴随账号迁移、活动取消等截图。",
        "risk": "high", "status": "rising", "keyword": "星野",
        "platforms": 2, "evidence": 19, "interactions": 23110, "trend": 56,
        "created_at": _ago_ms(4),
    },
    {
        "id": 4, "title": "某主播跳槽事件",
        "excerpt": "粉丝对骂、平台客服介入、相关视频大量二次创作。",
        "risk": "mid", "status": "cooling", "keyword": "主播",
        "platforms": 4, "evidence": 33, "interactions": 18900, "trend": -22,
        "created_at": _ago_ms(20),
    },
    {
        "id": 5, "title": "行业安全标准更新",
        "excerpt": "权威媒体解读 + 多位 KOL 跟进，影响范围广。",
        "risk": "low", "status": "stable", "keyword": "安全标准",
        "platforms": 6, "evidence": 12, "interactions": 4530, "trend": 4,
        "created_at": _ago_ms(36),
    },
    {
        "id": 6, "title": "某明星婚讯曝光",
        "excerpt": "娱乐类话题，跨平台热榜齐进，互动量级大。",
        "risk": "low", "status": "stable", "keyword": "明星",
        "platforms": 7, "evidence": 88, "interactions": 102000, "trend": 18,
        "created_at": _ago_ms(12),
    },
]


class EventsStore:
    def list(self, risk: Optional[str] = None, status: Optional[str] = None) -> dict:
        items = list(_EVENTS)
        if risk and risk != "all":
            items = [e for e in items if e["risk"] == risk]
        if status and status != "all":
            items = [e for e in items if e["status"] == status]
        return {"total": len(items), "items": items}

    def detail(self, id: int | str) -> dict:
        e = next((x for x in _EVENTS if str(x["id"]) == str(id)), _EVENTS[0])
        return {
            **e,
            "timeline": [
                {"at": _ago_ms(48), "label": "话题出现"},
                {"at": _ago_ms(24), "label": "跨平台扩散"},
                {"at": _ago_ms(6), "label": "进入热榜"},
                {"at": _ago_ms(1), "label": "高风险提示"},
            ],
            "sources": [
                {"platform": "小红书", "count": 12},
                {"platform": "B站", "count": 8},
                {"platform": "抖音", "count": 5},
                {"platform": "微博", "count": 3},
            ],
        }


events = EventsStore()


# ---------- Dashboard ----------

_DASHBOARD = {
    "active_events": 35,
    "active_events_delta": "+12%",
    "interactions_today": 1_284_320,
    "interactions_today_delta": "+4.2%",
    "high_risk": 6,
    "high_risk_delta": "+2",
    "accounts": 142,
    "accounts_delta": "+3",
    "recent_high": [
        {"id": 1, "keyword": "星野", "risk": "high", "status": "rising", "platforms": 3, "evidence": 28, "interactions": 12_400},
        {"id": 2, "keyword": "某品牌负面", "risk": "high", "status": "stable", "platforms": 5, "evidence": 64, "interactions": 56_200},
        {"id": 3, "keyword": "新品发布", "risk": "mid", "status": "rising", "platforms": 2, "evidence": 14, "interactions": 8_100},
        {"id": 4, "keyword": "主播跳槽", "risk": "mid", "status": "cooling", "platforms": 4, "evidence": 33, "interactions": 18_900},
        {"id": 5, "keyword": "行业安全标准", "risk": "low", "status": "stable", "platforms": 6, "evidence": 12, "interactions": 4_530},
    ],
}


class DashboardStore:
    def metrics(self) -> dict:
        return dict(_DASHBOARD)


dashboard = DashboardStore()


# ---------- Comments ----------

_COMMENTS = [
    {"id": 1, "platform": "小红书", "content": "已经退款，态度太离谱了。", "author": "小橘子", "post_title": "某品牌新品首发体验", "sentiment": "negative", "status": "pending", "created_at": _ago_ms(1)},
    {"id": 2, "platform": "B站", "content": "实测没翻车，参数很顶。", "author": "硬件菌", "post_title": "新机评测", "sentiment": "positive", "status": "replied", "created_at": _ago_ms(2)},
    {"id": 3, "platform": "抖音", "content": "价格还行，等等党赢了。", "author": "日常打卡", "post_title": "新品开箱视频", "sentiment": "neutral", "status": "pending", "created_at": _ago_ms(3)},
    {"id": 4, "platform": "微博", "content": "客服已联系，给个机会。", "author": "吃瓜群众", "post_title": "某品牌新品首发体验", "sentiment": "neutral", "status": "replied", "created_at": _ago_ms(5)},
    {"id": 5, "platform": "小红书", "content": "求链接求同款！", "author": "种草机", "post_title": "新品开箱视频", "sentiment": "positive", "status": "pending", "created_at": _ago_ms(7)},
    {"id": 6, "platform": "快手", "content": "广告太多，体验差。", "author": "老铁来了", "post_title": "某品牌新品首发体验", "sentiment": "negative", "status": "hidden", "created_at": _ago_ms(9)},
]


class CommentsStore:
    def list(self, platform: Optional[str] = None, status: Optional[str] = None) -> dict:
        items = list(_COMMENTS)
        if platform and platform != "all":
            items = [c for c in items if c["platform"] == platform]
        if status and status != "all":
            items = [c for c in items if c["status"] == status]
        return {"total": len(items), "items": items}

    def reply(self, id: int | str, text: str) -> dict:
        for c in _COMMENTS:
            if str(c["id"]) == str(id):
                c["status"] = "replied"
        return {"ok": True}

    def hide(self, id: int | str) -> dict:
        for c in _COMMENTS:
            if str(c["id"]) == str(id):
                c["status"] = "hidden"
        return {"ok": True}


comments = CommentsStore()


# ---------- Library ----------

_LIBRARY = [
    {"id": 1, "platform": "小红书", "title": "星野还能玩吗？最新攻略", "author": "攻略君", "url": "https://www.xiaohongshu.com/explore/1", "likes": 3210, "comments": 412, "collected_at": _ago_ms(2), "tags": ["游戏", "攻略"]},
    {"id": 2, "platform": "B站", "title": "深度测评：新一代旗舰芯片", "author": "硬件菌", "url": "https://www.bilibili.com/video/BV1", "likes": 12_400, "comments": 1_200, "collected_at": _ago_ms(3), "tags": ["数码", "评测"]},
    {"id": 3, "platform": "抖音", "title": "新品开箱：3 分钟看完", "author": "日常打卡", "url": "https://www.douyin.com/video/1", "likes": 56_200, "comments": 3_400, "collected_at": _ago_ms(4), "tags": ["开箱"]},
    {"id": 4, "platform": "微博", "title": "某明星婚讯曝光", "author": "娱乐速报", "url": "https://weibo.com/1", "likes": 102_000, "comments": 8_900, "collected_at": _ago_ms(12), "tags": ["娱乐"]},
    {"id": 5, "platform": "快手", "title": "行业安全标准最新解读", "author": "安全卫士", "url": "https://www.kuaishou.com/1", "likes": 4_530, "comments": 220, "collected_at": _ago_ms(36), "tags": ["行业", "安全"]},
    {"id": 6, "platform": "YouTube", "title": "How to use Sonar", "author": "Sonar Official", "url": "https://youtube.com/watch?v=1", "likes": 1_240, "comments": 88, "collected_at": _ago_ms(48), "tags": ["教程"]},
]


class LibraryStore:
    def list(self, q: Optional[str] = None, platform: Optional[str] = None) -> dict:
        items = list(_LIBRARY)
        if platform and platform != "all":
            items = [x for x in items if x["platform"] == platform]
        if q:
            ql = q.lower()
            items = [x for x in items if ql in x["title"].lower() or ql in x["author"].lower()]
        return {"total": len(items), "items": items}


library = LibraryStore()


# ---------- Insights ----------

_INSIGHTS = {
    "trend_series": [
        {"day": "D-13", "events": 12, "interactions": 38000},
        {"day": "D-12", "events": 14, "interactions": 42100},
        {"day": "D-11", "events": 10, "interactions": 39500},
        {"day": "D-10", "events": 18, "interactions": 48200},
        {"day": "D-9",  "events": 16, "interactions": 44300},
        {"day": "D-8",  "events": 22, "interactions": 51200},
        {"day": "D-7",  "events": 19, "interactions": 49800},
        {"day": "D-6",  "events": 25, "interactions": 55400},
        {"day": "D-5",  "events": 21, "interactions": 52100},
        {"day": "D-4",  "events": 28, "interactions": 58600},
        {"day": "D-3",  "events": 24, "interactions": 56700},
        {"day": "D-2",  "events": 31, "interactions": 61200},
        {"day": "D-1",  "events": 28, "interactions": 59500},
        {"day": "D-0",  "events": 36, "interactions": 64400},
    ],
    "brand_compare": [
        {"brand": "品牌A", "score": 82, "delta": "+5"},
        {"brand": "品牌B", "score": 76, "delta": "-2"},
        {"brand": "品牌C", "score": 71, "delta": "+1"},
        {"brand": "品牌D", "score": 64, "delta": "+3"},
        {"brand": "品牌E", "score": 58, "delta": "-1"},
    ],
    "audiences": [
        {"tag": "一线 · 女性", "share": 32},
        {"tag": "一线 · 男性", "share": 18},
        {"tag": "新一线 · 女性", "share": 24},
        {"tag": "新一线 · 男性", "share": 11},
        {"tag": "其他", "share": 15},
    ],
}


class InsightsStore:
    def all(self) -> dict:
        return _INSIGHTS


insights = InsightsStore()


# ---------- Lexicon ----------

_LEXICON = [
    {"id": 1, "term": "星野", "kind": "keyword", "weight": 1.0, "updated_at": _ago_ms(8)},
    {"id": 2, "term": "跑路", "kind": "sensitive", "weight": 0.9, "updated_at": _ago_ms(20)},
    {"id": 3, "term": "的", "kind": "stopword", "updated_at": _ago_ms(120)},
    {"id": 4, "term": "游戏=Game", "kind": "synonym", "updated_at": _ago_ms(60)},
    {"id": 5, "term": "主播", "kind": "keyword", "weight": 0.7, "updated_at": _ago_ms(30)},
    {"id": 6, "term": "负面", "kind": "sensitive", "weight": 0.85, "updated_at": _ago_ms(15)},
]


class LexiconStore:
    def list(self) -> list[dict]:
        return list(_LEXICON)

    def create(self, item: dict) -> dict:
        nid = _next_id(_LEXICON)
        new = {"id": nid, "updated_at": _now_ms(), **item}
        _LEXICON.append(new)
        return new

    def delete(self, id: int | str) -> dict:
        for i, x in enumerate(_LEXICON):
            if str(x["id"]) == str(id):
                _LEXICON.pop(i)
                return {"ok": True}
        return {"ok": False}


lexicon = LexiconStore()


# ---------- Scheduled ----------

_SCHEDULED = [
    {"id": 1, "name": "小红书热门抓取", "cron": "0 */2 * * *", "enabled": True, "last_run": _ago_ms(1), "last_status": "success"},
    {"id": 2, "name": "B站热门抓取", "cron": "0 */3 * * *", "enabled": True, "last_run": _ago_ms(2), "last_status": "success"},
    {"id": 3, "name": "事件聚合分析", "cron": "0 */6 * * *", "enabled": True, "last_run": _ago_ms(3), "last_status": "success"},
    {"id": 4, "name": "账号健康巡检", "cron": "0 9 * * *", "enabled": False, "last_run": _ago_ms(48), "last_status": "failed"},
    {"id": 5, "name": "飞书通知发送", "cron": "*/30 * * * *", "enabled": True, "last_run": _ago_ms(0), "last_status": "running"},
]


class ScheduledStore:
    def list(self) -> list[dict]:
        return list(_SCHEDULED)

    def toggle(self, id: int | str, enabled: bool) -> dict:
        for t in _SCHEDULED:
            if str(t["id"]) == str(id):
                t["enabled"] = enabled
        return {"ok": True}


scheduled = ScheduledStore()


# ---------- Agent ----------

_AGENT = [
    {"id": 1, "name": "研判：星野事件", "status": "success", "input": 'topic="星野"', "output": "高风险，建议 24h 内响应…", "created_at": _ago_ms(1), "duration_ms": 12_400},
    {"id": 2, "name": "周报：行业洞察", "status": "success", "input": "period=last_week", "output": "本周热榜 Top 3…", "created_at": _ago_ms(6), "duration_ms": 38_200},
    {"id": 3, "name": "回复草稿：品牌A", "status": "failed", "input": "post_id=123", "output": "生成失败：上下文过长", "created_at": _ago_ms(8)},
    {"id": 4, "name": "回复草稿：品牌B", "status": "running", "input": "post_id=124", "created_at": _ago_ms(0)},
    {"id": 5, "name": "研判：主播跳槽", "status": "queued", "input": 'topic="主播"', "created_at": _ago_ms(0)},
]


class AgentStore:
    def list(self) -> list[dict]:
        return list(_AGENT)


agent_tasks = AgentStore()


# ---------- Accounts ----------

_ACCOUNTS = [
    {"id": 1, "platform": "小红书", "name": "主号-品牌A", "status": "healthy", "last_used": _ago_ms(1), "requests_today": 1240},
    {"id": 2, "platform": "小红书", "name": "备用-品牌A", "status": "warning", "last_used": _ago_ms(6), "requests_today": 220},
    {"id": 3, "platform": "B站", "name": "主号-品牌A", "status": "healthy", "last_used": _ago_ms(0), "requests_today": 3120},
    {"id": 4, "platform": "抖音", "name": "主号-品牌A", "status": "expired", "last_used": _ago_ms(96), "requests_today": 0},
    {"id": 5, "platform": "微博", "name": "主号-品牌A", "status": "healthy", "last_used": _ago_ms(2), "requests_today": 980},
    {"id": 6, "platform": "YouTube", "name": "主号-品牌A", "status": "warning", "last_used": _ago_ms(12), "requests_today": 60},
]

_PROXIES = [
    {"id": 1, "name": "HK-01", "scheme": "http", "host": "10.0.0.1", "port": 7890, "status": "healthy", "latency_ms": 80},
    {"id": 2, "name": "JP-01", "scheme": "socks5", "host": "10.0.0.2", "port": 1080, "status": "healthy", "latency_ms": 120},
    {"id": 3, "name": "US-01", "scheme": "http", "host": "10.0.0.3", "port": 7890, "status": "warning", "latency_ms": 480},
    {"id": 4, "name": "SG-01", "scheme": "socks5", "host": "10.0.0.4", "port": 1080, "status": "expired", "latency_ms": 0},
]


class AccountsStore:
    def list(self) -> list[dict]:
        return list(_ACCOUNTS)

    def create(self, item: dict) -> dict:
        nid = _next_id(_ACCOUNTS)
        new = {"id": nid, "requests_today": 0, **item}
        _ACCOUNTS.append(new)
        return new

    def update(self, id: int | str, patch: dict) -> dict:
        for a in _ACCOUNTS:
            if str(a["id"]) == str(id):
                a.update(patch)
                return a
        return {}

    def delete(self, id: int | str) -> dict:
        for i, a in enumerate(_ACCOUNTS):
            if str(a["id"]) == str(id):
                _ACCOUNTS.pop(i)
                return {"ok": True}
        return {"ok": False}


accounts = AccountsStore()


class ProxiesStore:
    def list(self) -> list[dict]:
        return list(_PROXIES)

    def create(self, item: dict) -> dict:
        nid = _next_id(_PROXIES)
        new = {"id": nid, "latency_ms": 0, **item}
        _PROXIES.append(new)
        return new

    def update(self, id: int | str, patch: dict) -> dict:
        for p in _PROXIES:
            if str(p["id"]) == str(id):
                p.update(patch)
                return p
        return {}

    def delete(self, id: int | str) -> dict:
        for i, p in enumerate(_PROXIES):
            if str(p["id"]) == str(id):
                _PROXIES.pop(i)
                return {"ok": True}
        return {"ok": False}


proxies = ProxiesStore()


# ---------- Settings ----------

_SETTINGS = {
    "notify_feishu": True,
    "notify_email": False,
    "notify_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxx",
    "default_team": "所有团队",
    "risk_threshold": 70,
    "scrape_interval_min": 30,
}


class SettingsStore:
    def get(self) -> dict:
        return dict(_SETTINGS)

    def save(self, data: dict) -> dict:
        _SETTINGS.update(data)
        return dict(_SETTINGS)


settings = SettingsStore()
