#!/usr/bin/env python3
"""诊断脚本：把平台页面里所有跟数据相关的全局变量都 dump 出来。"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser.pool import pool


# 每个平台要 inspect 的 JS
INSPECT_JS = r"""
() => {
  const out = {
    url: location.href,
    title: document.title,
    has_router_data: false,
    router_data_len: 0,
    has_initial_state: false,
    initial_state_keys: [],
    has_apollo: false,
    apollo_keys_count: 0,
    apollo_sample: null,
    has_sigi: false,
    sigi_keys: [],
    has_redux: false,
    has_login_form: !!document.querySelector('input[type="password"]') || !!document.querySelector('input[placeholder*="手机"]') || !!document.querySelector('[class*="login"]'),
    body_text_sample: document.body ? document.body.innerText.slice(0, 200) : '',
  };
  try {
    if (window._ROUTER_DATA) {
      out.has_router_data = true;
      const s = typeof window._ROUTER_DATA === 'string' ? window._ROUTER_DATA : JSON.stringify(window._ROUTER_DATA);
      out.router_data_len = s.length;
    }
  } catch (e) {}
  try {
    if (window.__INITIAL_STATE__) {
      out.has_initial_state = true;
      out.initial_state_keys = Object.keys(window.__INITIAL_STATE__).slice(0, 20);
    }
  } catch (e) {}
  try {
    if (window.__APOLLO_STATE__) {
      out.has_apollo = true;
      out.apollo_keys_count = Object.keys(window.__APOLLO_STATE__).length;
      const keys = Object.keys(window.__APOLLO_STATE__);
      out.apollo_sample = keys.slice(0, 5);
    }
  } catch (e) {}
  try {
    if (window.SIGI_STATE) {
      out.has_sigi = true;
      out.sigi_keys = Object.keys(window.SIGI_STATE).slice(0, 20);
    }
  } catch (e) {}
  return out;
}
"""


async def diagnose(platform: str, url: str):
    print(f"\n{'='*60}\n[{platform}] navigate {url}\n{'='*60}")
    try:
        await pool.navigate(platform, url, wait_until="domcontentloaded", timeout_ms=20000)
    except Exception as e:
        print(f"  navigate failed: {e}")
        return
    try:
        result = await pool.evaluate(platform, INSPECT_JS, timeout_ms=10000)
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"  evaluate failed: {e}")


async def main():
    await pool.start()
    await diagnose("douyin", "https://www.douyin.com/search/咖啡?type=general")
    await diagnose("kuaishou", "https://www.kuaishou.com/search/visionnew?searchKey=咖啡")
    await diagnose("xhs", "https://www.xiaohongshu.com/search_result?keyword=咖啡&source=web_explore_feed&type=51")
    await pool.stop()


if __name__ == "__main__":
    asyncio.run(main())
