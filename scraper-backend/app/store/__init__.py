"""内存版业务层数据 store。

为前端 12 个业务模块提供 mock 数据 + 内存 CRUD。
刷新后端进程会重置。生产环境应替换为真实数据库。
"""

from .data import (
    events,
    teams,
    dashboard,
    comments,
    library,
    insights,
    lexicon,
    scheduled,
    agent_tasks,
    accounts,
    proxies,
    settings,
    auth,
)

__all__ = [
    "events",
    "teams",
    "dashboard",
    "comments",
    "library",
    "insights",
    "lexicon",
    "scheduled",
    "agent_tasks",
    "accounts",
    "proxies",
    "settings",
    "auth",
]
