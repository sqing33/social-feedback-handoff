"""POST /api/proxies/test — 代理测试（§6.3）。"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, Request

from ..errors import ErrorCode, ScraperError
from ..models import ProxyTestRequest

router = APIRouter()

CHECK_URL = "https://www.baidu.com/"
IP_PROBE_URL = "https://api.ipify.org?format=json"
GEO_PROBE_URL = "http://ip-api.com/json/{ip}"


@router.post("/api/proxies/test")
async def test_proxy(body: ProxyTestRequest, request: Request) -> dict:
    p = body.proxy_context
    proxy_url = p.url or f"{p.scheme}://{p.host}:{p.port}"
    if p.username and p.password and "://" in proxy_url and "@" not in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        proxy_url = f"{scheme}://{p.username}:{p.password}@{rest}"

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            # 1) 连通性
            try:
                resp = await client.get(CHECK_URL, proxy=proxy_url)
                status_code = resp.status_code
                if status_code == 407 or status_code == 502 or status_code == 504:
                    raise ScraperError(
                        f"Proxy auth/connection failed: {status_code}",
                        code=ErrorCode.PROXY_ERROR,
                        status_code=502,
                        retryable=False,
                        capability="proxy",
                    )
            except httpx.RequestError as e:
                raise ScraperError(
                    f"Proxy connect failed: {e}",
                    code=ErrorCode.PROXY_ERROR,
                    status_code=502,
                    retryable=False,
                    capability="proxy",
                )

            # 2) 出口 IP
            outbound_ip: str | None = None
            outbound_ip_error: str | None = None
            try:
                r = await client.get(IP_PROBE_URL, proxy=proxy_url)
                outbound_ip = r.json().get("ip")
            except Exception as e:
                outbound_ip_error = str(e)

            # 3) 地理信息
            geo: dict | None = None
            geo_error: str | None = None
            if outbound_ip:
                try:
                    r = await client.get(GEO_PROBE_URL.format(ip=outbound_ip), proxy=proxy_url)
                    d = r.json()
                    geo = {
                        "country": d.get("country"),
                        "region": d.get("regionName"),
                        "city": d.get("city"),
                        "timezone": d.get("timezone"),
                        "asn": d.get("as"),
                        "isp": d.get("isp"),
                    }
                except Exception as e:
                    geo_error = str(e)
    except ScraperError:
        raise
    except Exception as e:
        raise ScraperError(
            f"Proxy test failed: {e}",
            code=ErrorCode.SCRAPE_ERROR,
            status_code=500,
            retryable=False,
            capability="proxy",
        )

    latency_ms = int((time.time() - t0) * 1000)
    return {
        "success": True,
        "latency_ms": latency_ms,
        "check_url": CHECK_URL,
        "status_code": status_code,
        "outbound_ip": outbound_ip,
        "outbound_ip_error": outbound_ip_error,
        "geo": geo,
        "geo_error": geo_error,
        "ip_probes": [],
        "geo_probes": [],
        "connectivity_probes": [],
    }
