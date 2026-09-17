"""运行配置：从环境变量读取。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    host: str
    port: int
    api_key: str
    log_level: str
    formal_mode: bool
    enable_legacy_api: bool


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    return Settings(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8007")),
        api_key=os.environ.get("SCRAPER_API_KEY", "").strip(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        formal_mode=_env_bool("FORMAL_MODE", True),
        enable_legacy_api=_env_bool("ENABLE_LEGACY_API", False),
    )


settings = load_settings()
