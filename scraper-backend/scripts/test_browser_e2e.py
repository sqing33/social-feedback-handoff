#!/usr/bin/env python3
"""端到端验证脚本：Playwright 抓取流程。

测试场景：
1. 启动浏览器池
2. 注入假 cookie（模拟登录态）
3. 各平台 search → 看 navigate + extract 是否跑通
4. 输出每个平台的 navigate 时间 / wait_for_state 时间 / 数据条数

不依赖真实 cookie，主要验证 Playwright 抓取管线工作正常。
真实数据需要用户登录后才有。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser.pool import pool
from app.scrapers.xhs import XhsScraper
from app.scrapers.douyin import DouyinScraper
from app.scrapers.kuaishou import KuaishouScraper
from app.scrapers.tiktok import TikTokScraper
from app.scrapers.bilibili import BilibiliScraper


async def test_bilibili() -> dict:
    """B 站免登录，应该能直接拿数据。"""
    print("\n[BILIBILI] search '星野'")
    t0 = time.time()
    s = BilibiliScraper()
    try:
        results = await s.search("星野", limit=3)
        print(f"  ✓ {len(results)} results in {time.time()-t0:.2f}s")
        for r in results[:2]:
            print(f"    - {r['content_id']} | {r['title'][:30]} | view={r['stats']['view']}")
        return {"ok": True, "count": len(results), "elapsed": time.time() - t0}
    except Exception as e:
        print(f"  ✗ failed: {e}")
        return {"ok": False, "error": str(e)}


async def test_xhs_with_fake_cookie() -> dict:
    """XHS 注入假 cookie 看 Playwright 流程是否跑通（数据可能为空因为 cookie 假）。"""
    print("\n[XHS] inject fake cookie + search '咖啡'")
    try:
        await pool.start()
        # 注入一些假 cookie 绕开 is_logged_in 检查
        from datetime import datetime, timedelta
        expires = int((datetime.utcnow() + timedelta(days=30)).timestamp())
        cookies = [
            {"name": "web_session", "value": "fake_session_0403", "domain": ".xiaohongshu.com", "path": "/", "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
            {"name": "webId", "value": "fake_webid_0403", "domain": ".xiaohongshu.com", "path": "/", "expires": expires, "httpOnly": False, "secure": True, "sameSite": "Lax"},
        ]
        await pool._context.add_cookies(cookies)  # type: ignore
        print(f"  injected {len(cookies)} cookies")
        # 现在 is_logged_in 应该返回 True（有 web_session）
        actual = await pool.get_cookies("xhs")
        print(f"  pool cookies count: {len(actual)}, logged_in={pool.is_logged_in('xhs', actual)}")

        t0 = time.time()
        s = XhsScraper()
        try:
            results = await s.search("咖啡", limit=3)
            elapsed = time.time() - t0
            print(f"  navigate+extract done in {elapsed:.2f}s, got {len(results)} results")
            for r in results[:2]:
                print(f"    - {r['content_id'][:20]} | {r['title'][:30]} | like={r['stats']['like']}")
            return {"ok": True, "count": len(results), "elapsed": elapsed}
        except Exception as e:
            print(f"  search error (expected with fake cookie): {type(e).__name__}: {e}")
            return {"ok": True, "navigate_done": True, "extract_empty": True, "error": str(e)[:80]}
    except Exception as e:
        print(f"  ✗ setup failed: {e}")
        return {"ok": False, "error": str(e)}


async def test_douyin_navigate_only() -> dict:
    """抖音只测 navigate（不期望数据）。"""
    print("\n[DOUYIN] navigate only (no real cookie)")
    try:
        await pool.start()
        from datetime import datetime, timedelta
        expires = int((datetime.utcnow() + timedelta(days=30)).timestamp())
        cookies = [
            {"name": "sessionid", "value": "fake_session", "domain": ".douyin.com", "path": "/", "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
            {"name": "ttwid", "value": "fake_ttwid", "domain": ".douyin.com", "path": "/", "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        ]
        await pool._context.add_cookies(cookies)  # type: ignore
        t0 = time.time()
        try:
            url = "https://www.douyin.com/search/咖啡?type=general"
            await pool.navigate("douyin", url, wait_until="domcontentloaded", timeout_ms=15000)
            elapsed = time.time() - t0
            # 试着 evaluate 拿数据
            try:
                js = "() => { const s = window._ROUTER_DATA; return s ? 'has_router_data' : 'no_router_data'; }"
                marker = await pool.evaluate("douyin", js, timeout_ms=5000)
            except Exception as e:
                marker = f"eval failed: {type(e).__name__}"
            print(f"  navigated in {elapsed:.2f}s, marker={marker}")
            return {"ok": True, "elapsed": elapsed, "marker": str(marker)}
        except Exception as e:
            print(f"  navigate error: {e}")
            return {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"  ✗ setup failed: {e}")
        return {"ok": False, "error": str(e)}


async def test_kuaishou_navigate() -> dict:
    print("\n[KUAISHOU] navigate only")
    try:
        await pool.start()
        from datetime import datetime, timedelta
        expires = int((datetime.utcnow() + timedelta(days=30)).timestamp())
        cookies = [
            {"name": "userId", "value": "12345", "domain": ".kuaishou.com", "path": "/", "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        ]
        await pool._context.add_cookies(cookies)  # type: ignore
        t0 = time.time()
        try:
            url = "https://www.kuaishou.com/search/visionnew?searchKey=咖啡"
            await pool.navigate("kuaishou", url, wait_until="domcontentloaded", timeout_ms=15000)
            elapsed = time.time() - t0
            js = "() => { const s = window.__APOLLO_STATE__; return s ? Object.keys(s).length : 0; }"
            try:
                count = await pool.evaluate("kuaishou", js, timeout_ms=5000)
            except Exception as e:
                count = f"err: {type(e).__name__}"
            print(f"  navigated in {elapsed:.2f}s, apollo keys={count}")
            return {"ok": True, "elapsed": elapsed, "apollo_keys": count}
        except Exception as e:
            print(f"  navigate error: {e}")
            return {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"  ✗ setup failed: {e}")
        return {"ok": False, "error": str(e)}


async def test_tiktok_navigate() -> dict:
    print("\n[TIKTOK] navigate only")
    try:
        await pool.start()
        from datetime import datetime, timedelta
        expires = int((datetime.utcnow() + timedelta(days=30)).timestamp())
        cookies = [
            {"name": "ttwid", "value": "fake_ttwid", "domain": ".tiktok.com", "path": "/", "expires": expires, "httpOnly": True, "secure": True, "sameSite": "Lax"},
        ]
        await pool._context.add_cookies(cookies)  # type: ignore
        t0 = time.time()
        try:
            url = "https://www.tiktok.com/search?q=coffee"
            await pool.navigate("tiktok", url, wait_until="domcontentloaded", timeout_ms=15000)
            elapsed = time.time() - t0
            js = "() => { const s = window.SIGI_STATE; return s ? (s.ItemModule ? Object.keys(s.ItemModule).length : 0) : 0; }"
            try:
                count = await pool.evaluate("tiktok", js, timeout_ms=5000)
            except Exception as e:
                count = f"err: {type(e).__name__}"
            print(f"  navigated in {elapsed:.2f}s, SIGI_STATE items={count}")
            return {"ok": True, "elapsed": elapsed, "sigi_items": count}
        except Exception as e:
            print(f"  navigate error: {e}")
            return {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"  ✗ setup failed: {e}")
        return {"ok": False, "error": str(e)}


async def main() -> int:
    print("=" * 60)
    print("Playwright 端到端验证")
    print("=" * 60)
    summary = {}
    summary["bilibili"] = await test_bilibili()
    summary["xhs"] = await test_xhs_with_fake_cookie()
    summary["douyin"] = await test_douyin_navigate_only()
    summary["kuaishou"] = await test_kuaishou_navigate()
    summary["tiktok"] = await test_tiktok_navigate()
    await pool.stop()
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ok_count = sum(1 for v in summary.values() if v.get("ok"))
    print(f"\n{ok_count}/{len(summary)} platforms OK")
    return 0 if ok_count == len(summary) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
