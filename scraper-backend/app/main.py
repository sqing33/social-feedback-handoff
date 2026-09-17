"""FastAPI scraper application entry point.

Formal mode registers only real scraping, real browser credential, status,
probe, and minimal browser login routes. Legacy Sonar compatibility routes are
available only when ENABLE_LEGACY_API=1 is explicitly set.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .browser import router as browser_router
from .config import settings
from .errors import (
    ScraperError,
    http_exception_handler,
    scraper_exception_handler,
    validation_exception_handler,
)
from .middleware import APIKeyMiddleware, RequestIDMiddleware
from .routers import credentials, scrape, scrapers, status
from .xhs_bridge.server import bridge_from_environment

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scraper")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bridge = bridge_from_environment()
    if bridge is not None:
        try:
            await bridge.start()
            logger.info("safe XHS Bridge enabled on loopback")
        except OSError:
            logger.error("safe XHS Bridge port is unavailable; continuing with Playwright fallback")
            bridge = None
    try:
        yield
    finally:
        if bridge is not None:
            await bridge.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Social Feedback Real Scraper",
        version="1.0.0",
        description="真实平台公开内容抓取服务；正式模式禁止 mock 数据降级。",
        lifespan=lifespan,
    )

    app.add_middleware(APIKeyMiddleware, api_key=settings.api_key)
    app.add_middleware(RequestIDMiddleware)

    app.add_exception_handler(ScraperError, scraper_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    # Formal routes: real scraper and controlled browser login only.
    app.include_router(status.router)
    app.include_router(scrape.router)
    app.include_router(scrapers.router)
    app.include_router(credentials.router)
    app.include_router(browser_router.router)

    if settings.enable_legacy_api:
        logger.warning("ENABLE_LEGACY_API=1: legacy compatibility routes are enabled")
        from .bridge import http_bridge
        from .routers import auth, business, proxies, reload

        app.include_router(reload.router)
        app.include_router(proxies.router)
        app.include_router(auth.router)
        app.include_router(business.router)
        app.include_router(http_bridge.router)

    @app.get("/")
    def root():
        return {
            "service": "Social Feedback Real Scraper",
            "version": "1.0.0",
            "formal_mode": settings.formal_mode,
            "legacy_api_enabled": settings.enable_legacy_api,
            "docs": "/docs",
            "status": "/api/status",
        }

    return app


app = create_app()
