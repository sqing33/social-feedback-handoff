"""平台 cookie / token 加载。

优先级：
1. 请求体里的 auth_context.cookie（每次请求覆盖）
2. 进程内 COOKIES_STORE（可在 /api/credentials/... 动态注册）
3. 环境变量 COOKIES_<PLATFORM>
"""

from __future__ import annotations

import os
import threading
from typing import Optional


_lock = threading.RLock()
_store: dict[str, str] = {}


def set_cookie(platform: str, cookie: str) -> None:
    with _lock:
        if cookie:
            _store[platform] = cookie
        else:
            _store.pop(platform, None)


def get_cookie(platform: str) -> Optional[str]:
    with _lock:
        env_key = f"COOKIES_{platform.upper()}"
        if env_key in os.environ and os.environ[env_key].strip():
            return os.environ[env_key].strip()
        return _store.get(platform)


def clear_all() -> None:
    with _lock:
        _store.clear()
