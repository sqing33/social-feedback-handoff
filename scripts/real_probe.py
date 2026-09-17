#!/usr/bin/env python3
"""Read-only real-data probes for XHS, Douyin, and Kuaishou.

The probe calls scraper endpoints only and never writes to the panel database.
URLs are read from environment variables so XHS xsec_token values are not stored
in source control:

  PROBE_XHS_URL=... PROBE_DOUYIN_URL=... PROBE_KUAISHOU_URL=... \
    ./scripts/run-real-probes.sh
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProbeTarget:
    platform: str
    label: str
    env_name: str
    url: str
    content_id: str
    xsec_token: str = ""
    xsec_source: str = ""


def _target(platform: str, label: str, env_name: str, url: str) -> ProbeTarget:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if platform == "xhs":
        match = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]+)", parsed.path)
        content_id = match.group(1) if match else ""
        token = (query.get("xsec_token") or [""])[0]
        source = (query.get("xsec_source") or ["pc_note"])[0]
        identifier = content_id
        if token:
            identifier = f"{content_id}?{urlencode({'xsec_token': token, 'xsec_source': source})}"
        return ProbeTarget(platform, label, env_name, url, identifier, token, source)
    if platform == "douyin":
        match = re.search(r"/video/(\d+)", parsed.path)
        return ProbeTarget(platform, label, env_name, url, match.group(1) if match else "")
    if platform == "kuaishou":
        match = re.search(r"/short-video/([^/?#]+)", parsed.path)
        return ProbeTarget(platform, label, env_name, url, match.group(1) if match else "")
    raise ValueError(f"unsupported platform: {platform}")


def _request(base_url: str, endpoint: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "X-Request-ID": f"formal-probe-{payload['platform']}"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = Request(
        f"{base_url.rstrip('/')}{endpoint}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {raw[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"scraper unavailable: {exc}") from exc


def _assert_real_meta(meta: dict[str, Any], platform: str) -> None:
    backend = str(meta.get("backend") or "")
    if backend != f"{platform}-real":
        raise AssertionError(f"unexpected backend: {backend or 'missing'}")
    if meta.get("degraded"):
        raise AssertionError("degraded result is forbidden")
    if "mock" in json.dumps(meta, ensure_ascii=False).lower():
        raise AssertionError("mock marker found in response metadata")


def probe(target: ProbeTarget, base_url: str, api_key: str, min_comments: int) -> dict[str, Any]:
    if not target.url or not target.content_id:
        raise ValueError(f"{target.env_name} is missing or is not a supported detail URL")
    info = _request(
        base_url,
        "/api/scrape/info",
        {"platform": target.platform, "content_id": target.content_id, "extra": target.url},
        api_key,
    )
    note = info.get("note_info") or {}
    _assert_real_meta(info.get("meta") or {}, target.platform)
    title = str(note.get("title") or "").strip()
    if not title:
        raise AssertionError("real title is missing")
    if note.get("publish_time") is None:
        raise AssertionError("publish_time is missing for the probe target")
    metrics = note.get("interact_info") or {}
    for key, value in metrics.items():
        if value is not None and (not isinstance(value, int) or value < 0):
            raise AssertionError(f"invalid metric {key}={value!r}")
    if target.platform == "xhs" and metrics.get("view_count") is not None:
        raise AssertionError("XHS public view_count must remain NULL when not exposed")
    if target.platform == "kuaishou" and metrics.get("collected_count") is not None:
        raise AssertionError("Kuaishou public collected_count must remain NULL when not exposed")

    comments = _request(
        base_url,
        "/api/scrape/comments",
        {
            "platform": target.platform,
            "content_id": target.content_id.split("?", 1)[0],
            "xsec_token": target.xsec_token or None,
            "xsec_source": target.xsec_source or None,
            "extra": target.url,
            "kwargs": {"max_comments": max(min_comments, 5), "include_info": False, "reuse_current_page": True},
        },
        api_key,
    )
    _assert_real_meta(comments.get("meta") or {}, target.platform)
    rows = comments.get("comments") or []
    if len(rows) < min_comments:
        raise AssertionError(f"expected at least {min_comments} real comments, got {len(rows)}")
    if any(not str(item.get("content") or "").strip() for item in rows[:min_comments]):
        raise AssertionError("empty comment content found")
    return {
        "platform": target.label,
        "content_id": target.content_id.split("?", 1)[0],
        "title": title,
        "publish_time": note.get("publish_time"),
        "metrics": metrics,
        "comments": len(rows),
        "backend": (info.get("meta") or {}).get("backend"),
        "result": "pass",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run read-only real-data probes without writing the panel database")
    parser.add_argument("--base-url", default=os.getenv("SCRAPER_URL", "http://127.0.0.1:8007"))
    parser.add_argument("--min-comments", type=int, default=1)
    parser.add_argument("--platform", action="append", choices=["xhs", "douyin", "kuaishou"])
    args = parser.parse_args()
    configured = {
        "xhs": ("小红书", "PROBE_XHS_URL"),
        "douyin": ("抖音", "PROBE_DOUYIN_URL"),
        "kuaishou": ("快手", "PROBE_KUAISHOU_URL"),
    }
    selected = args.platform or list(configured)
    api_key = os.getenv("SCRAPER_API_KEY", "")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for platform in selected:
        label, env_name = configured[platform]
        try:
            target = _target(platform, label, env_name, os.getenv(env_name, "").strip())
            results.append(probe(target, args.base_url, api_key, max(0, args.min_comments)))
        except Exception as exc:
            failures.append({"platform": label, "result": "fail", "error": str(exc)})
    print(json.dumps({"results": results, "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
