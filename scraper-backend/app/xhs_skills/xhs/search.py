"""搜索 Feeds，对应 Go xiaohongshu/search.go。"""

from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .errors import NoFeedsError
from .human import sleep_random
from .selectors import FILTER_BUTTON, FILTER_PANEL
from .types import Feed, FilterOption
from .urls import make_search_url

logger = logging.getLogger(__name__)

# 筛选选项映射表：{筛选组索引: [文本, ...]}
_FILTER_OPTIONS: dict[int, list[str]] = {
    1: ["综合", "最新", "最多点赞", "最多评论", "最多收藏"],
    2: ["不限", "视频", "图文"],
    3: ["不限", "一天内", "一周内", "半年内"],
    4: ["不限", "已看过", "未看过", "已关注"],
    5: ["不限", "同城", "附近"],
}

# 从 __INITIAL_STATE__ 提取搜索结果的 JS
_EXTRACT_SEARCH_JS = """
(() => {
    const s = window.__INITIAL_STATE__?.search;
    if (!s?.feeds) return "";
    const data = s.feeds.value !== undefined ? s.feeds.value : s.feeds._value;
    return data ? JSON.stringify(data) : "";
})()
"""


def _find_internal_option(group_index: int, text: str) -> tuple[int, str]:
    """查找内部筛选选项。

    Returns:
        (filters_index, text)

    Raises:
        ValueError: 未找到匹配的选项。
    """
    options = _FILTER_OPTIONS.get(group_index)
    if not options:
        raise ValueError(f"筛选组 {group_index} 不存在")

    if text in options:
        return group_index, text

    raise ValueError(f"在筛选组 {group_index} 中未找到 '{text}'，有效值: {options}")


def _convert_filters(filter_opt: FilterOption) -> list[tuple[int, str]]:
    """将 FilterOption 转换为内部 (filters_index, text) 列表。"""
    result: list[tuple[int, str]] = []

    if filter_opt.sort_by:
        result.append(_find_internal_option(1, filter_opt.sort_by))
    if filter_opt.note_type:
        result.append(_find_internal_option(2, filter_opt.note_type))
    if filter_opt.publish_time:
        result.append(_find_internal_option(3, filter_opt.publish_time))
    if filter_opt.search_scope:
        result.append(_find_internal_option(4, filter_opt.search_scope))
    if filter_opt.location:
        result.append(_find_internal_option(5, filter_opt.location))

    return result


def search_feeds(
    page: Page,
    keyword: str,
    filter_option: FilterOption | None = None,
    max_results: int = 20,
) -> list[Feed]:
    """搜索 Feeds。

    Args:
        page: CDP 页面对象。
        keyword: 搜索关键词。
        filter_option: 可选筛选条件。
        max_results: 期望获取的最大结果数（会滚动加载直到达到或无更多）。

    Raises:
        NoFeedsError: 没有捕获到搜索结果。
        ValueError: 筛选选项无效。
    """
    search_url = make_search_url(keyword)
    page.navigate(search_url)
    page.wait_for_load()
    page.wait_dom_stable()

    # 等待 __INITIAL_STATE__.search.feeds 有数据
    _wait_for_search_feeds(page)

    # 应用筛选条件（若有）
    if filter_option:
        internal_filters = _convert_filters(filter_option)
        if internal_filters:
            _apply_filters(page, internal_filters)

    # 提取搜索结果（含翻页滚动加载）
    feeds = _extract_and_paginate(page, max_results)
    if not feeds:
        raise NoFeedsError()

    return feeds


def _extract_and_paginate(page: Page, max_results: int) -> list[Feed]:
    """提取搜索结果，如果不足 max_results 则向下滚动加载更多。

    小红书搜索结果是瀑布流：首屏约 20 条，向下滚动会触发 XHR 加载更多。
    通过 __INITIAL_STATE__ 的 feeds 数量变化判断是否有新数据。
    """
    seen_ids: set[str] = set()
    all_feeds: list[Feed] = []
    stagnant_rounds = 0
    max_scroll_rounds = 10  # safety limit

    for round_idx in range(max_scroll_rounds):
        result = page.evaluate(_EXTRACT_SEARCH_JS)
        if not result:
            break

        try:
            feeds_data = json.loads(result)
        except json.JSONDecodeError:
            break

        if not feeds_data:
            break

        new_added = 0
        for f in feeds_data:
            feed = Feed.from_dict(f)
            if feed.id and feed.id not in seen_ids:
                seen_ids.add(feed.id)
                all_feeds.append(feed)
                new_added += 1
            if len(all_feeds) >= max_results:
                break

        if len(all_feeds) >= max_results:
            break

        if new_added == 0:
            stagnant_rounds += 1
            if stagnant_rounds >= 2:
                logger.debug("search feeds stagnant after %d rounds, stopping", round_idx)
                break
        else:
            stagnant_rounds = 0

        # scroll down to trigger lazy load
        try:
            page.scroll_by(0, 2000)
        except Exception as exc:  # noqa: BLE001
            # 扩展把 scroll_by 收口到详情页评论容器(.note-scroller /
            # .interaction-container)，而搜索页没有这些容器，严格模式会抛
            # "评论滚动容器不可滚动或未就绪"。搜索瀑布流本来就该滚 window，
            # 这里退化用 evaluate 直接执行（evaluate 不走 domExecutor 的
            # 容器校验通道）。window 滚动失败则视为已到底，靠停滞计数退出。
            logger.debug("search scroll_by fell back to window.scrollBy: %s", exc)
            try:
                page.evaluate("(() => { window.scrollBy(0, 2000); return null; })()")
            except Exception:  # noqa: BLE001
                logger.debug("search window.scrollBy also failed; treat as bottom")
        sleep_random(500, 1000)

    logger.info("search extracted %d feeds (max_results=%d)", len(all_feeds), max_results)
    return all_feeds


def _wait_for_search_feeds(page: Page, timeout: float = 15.0) -> None:
    """等待 __INITIAL_STATE__.search.feeds 有数据。

    Raises:
        NoFeedsError: 超时仍无数据。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = page.evaluate(_EXTRACT_SEARCH_JS)
        if result:
            try:
                if json.loads(result):
                    return
            except json.JSONDecodeError:
                pass
        time.sleep(0.3)
    raise NoFeedsError()


def _apply_filters(page: Page, filters: list[tuple[int, str]]) -> None:
    """应用筛选条件。

    在单次 evaluate 调用内完成：点击筛选按钮 → 等待面板 → 按文本点击各选项
    → 等待搜索结果刷新。
    避免多次 WebSocket 连接导致面板关闭的时序问题。
    """
    filter_js_list = ", ".join(
        f'[{idx}, {json.dumps(text)}]' for idx, text in filters
    )

    # 记录当前 feeds 快照，用于检测结果是否已刷新
    snapshot_js = "JSON.stringify(window.__INITIAL_STATE__?.search?.feeds?.value ?? window.__INITIAL_STATE__?.search?.feeds?._value ?? null)"

    script = f"""
(() => {{
  return new Promise((resolve, reject) => {{
    const btn = document.querySelector('div.filter');
    if (!btn) {{ reject('筛选按钮不存在'); return; }}

    // 记录点击前的 feeds 快照（用于判断结果已刷新）
    const snapshot = {snapshot_js};
    // hover trigger (not click)
    ["mouseenter", "mouseover"].forEach(evt => btn.dispatchEvent(new Event(evt, {{ bubbles: true }})));

    const items = [{filter_js_list}];
    let attempts = 0;

    // 等待筛选面板出现
    const panelTimer = setInterval(() => {{
      const wrapper = document.querySelector('div.filters-wrapper');
      if (!wrapper) {{
        if (++attempts > 50) {{ clearInterval(panelTimer); reject('筛选面板等待超时'); }}
        return;
      }}
      clearInterval(panelTimer);

      // 依次点击各筛选项
      for (const [groupIdx, text] of items) {{
        const group = wrapper.querySelectorAll('div.filters')[groupIdx - 1];
        if (!group) {{ reject('筛选组 ' + groupIdx + ' 不存在'); return; }}
        const tag = Array.from(group.querySelectorAll('div.tags'))
          .find(el => el.textContent.trim() === text);
        if (!tag) {{ reject('选项不存在: ' + text); return; }}
        tag.click();
      }}

      // 等待搜索结果刷新（feeds 快照变化）
      let refreshAttempts = 0;
      const refreshTimer = setInterval(() => {{
        const current = {snapshot_js};
        if (current !== snapshot) {{
          clearInterval(refreshTimer);
          resolve(null);
          return;
        }}
        if (++refreshAttempts > 60) {{
          // 超时也继续（结果可能未变化）
          clearInterval(refreshTimer);
          resolve(null);
        }}
      }}, 100);
    }}, 100);
  }});
}})()
"""
    try:
        page.evaluate(script)
    except Exception as e:
        raise ValueError(f"应用筛选失败: {e}") from e

    # 等待 __INITIAL_STATE__ 中有新数据
    _wait_for_search_feeds(page)
