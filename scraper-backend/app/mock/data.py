"""Mock 平台数据生成。

每个平台都有：
- generate_note_info(platform, content_id) -> NoteInfo
- generate_comments(platform, content_id, max_comments=20) -> list[UnifiedComment]
- generate_search_results(platform, keyword, limit=20) -> list[SearchResult]

确定性：相同输入永远返回相同结果（用 hash 作 seed）。
"""

from __future__ import annotations

import hashlib
import time
import random
from typing import Optional

from ..models import (
    InteractInfo,
    NoteInfo,
    SearchResult,
    SearchStats,
    SubComment,
    UnifiedComment,
    UserInfo,
)


PLATFORMS = ["xhs", "bilibili", "twitter", "youtube", "douyin", "tiktok", "kuaishou"]


def _seed_for(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _rng(*parts: str) -> random.Random:
    return random.Random(_seed_for(*parts))


_PLATFORM_LABEL = {
    "xhs": "小红书",
    "bilibili": "B站",
    "twitter": "Twitter",
    "youtube": "YouTube",
    "douyin": "抖音",
    "tiktok": "TikTok",
    "kuaishou": "快手",
}


def _is_invalid_id(content_id: str) -> bool:
    """空、纯空白、太短都算无效。"""
    if not content_id or not content_id.strip():
        return True
    if len(content_id.strip()) < 2:
        return True
    return False


def _is_credential_expired(rng: random.Random) -> bool:
    return rng.random() < 0.05  # 5% 概率


def _is_rate_limited(rng: random.Random) -> bool:
    return rng.random() < 0.03  # 3% 概率


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_to_id(seed: int, length: int = 8) -> str:
    return hex(seed)[2:].rjust(length, "0")[:length]


def generate_note_info(platform: str, content_id: str) -> NoteInfo:
    rng = _rng(platform, "info", content_id)
    title_pool = [
        f"{_PLATFORM_LABEL.get(platform, platform)} · {content_id} 标题样例",
        f"这是一条关于 {content_id} 的精彩内容",
        f"用户分享：{content_id}",
    ]
    title = rng.choice(title_pool)
    nickname_pool = ["小明同学", "种草达人", "数码菌", "评测君", "路人甲", "星野玩家"]
    return NoteInfo(
        title=title,
        user=UserInfo(nickname=rng.choice(nickname_pool)),
        interact_info=InteractInfo(
            liked_count=rng.randint(100, 100_000),
            collected_count=rng.randint(10, 5_000),
            comment_count=rng.randint(0, 500),
        ),
    )


def generate_comments(platform: str, content_id: str, max_comments: int = 20) -> list[UnifiedComment]:
    rng = _rng(platform, "comments", content_id)
    count = min(max_comments, rng.randint(8, 15))
    nicknames = ["小橘子", "种草机", "硬件菌", "日常打卡", "吃瓜群众", "老铁来了", "技术宅", "路人甲"]
    contents = [
        "已经退款，态度太离谱了。",
        "实测没翻车，参数很顶。",
        "价格还行，等等党赢了。",
        "客服已联系，给个机会。",
        "求链接求同款！",
        "广告太多，体验差。",
        "支持国货，加油。",
        "没用过，坐等反馈。",
    ]
    out: list[UnifiedComment] = []
    for i in range(count):
        n_subs = rng.choice([0, 0, 0, 1, 2])
        subs = []
        for j in range(n_subs):
            subs.append(
                SubComment(
                    user_info=UserInfo(nickname=rng.choice(nicknames)),
                    content=rng.choice(contents),
                    like_count=rng.randint(0, 50),
                    create_time=_now_ms() - rng.randint(60, 3600) * 1000,
                )
            )
        out.append(
            UnifiedComment(
                user_info=UserInfo(nickname=rng.choice(nicknames)),
                content=rng.choice(contents),
                like_count=rng.randint(0, 1000),
                create_time=_now_ms() - rng.randint(60, 86400) * 1000,
                sub_comments=subs,
            )
        )
    return out


def generate_search_results(platform: str, keyword: str, limit: int = 20) -> list[SearchResult]:
    rng = _rng(platform, "search", keyword)
    count = min(limit, rng.randint(8, 12))
    out: list[SearchResult] = []
    for i in range(count):
        seed = _seed_for(platform, keyword, str(i))
        cid = _hash_to_id(seed, 12)
        item = SearchResult(
            content_id=cid,
            title=f"{keyword} · 样例结果 {i + 1}",
            author=rng.choice(["种草机", "硬件菌", "日常打卡", "评测君"]),
            url=f"https://example.com/{platform}/{cid}",
            stats=SearchStats(
                view=rng.randint(1000, 1_000_000),
                like=rng.randint(10, 50_000),
                reply=rng.randint(0, 2000),
            ),
        )
        # 平台原生 ID 字段
        if platform == "bilibili":
            item.bvid = "BV1" + _hash_to_id(seed, 10)
        elif platform == "xhs":
            item.note_id = cid
        elif platform == "twitter":
            item.tweet_id = cid
        elif platform in ("youtube", "tiktok"):
            item.video_id = cid
        elif platform == "douyin":
            item.aweme_id = cid
        out.append(item)
    return out


def generate_user_videos(platform: str, user_id: str, max_results: int = 5) -> list[SearchResult]:
    rng = _rng(platform, "user_videos", user_id)
    return generate_search_results(platform, f"user_{user_id}", max_results)[:max_results]


# 错误判定函数
def is_invalid_content_id(content_id: str) -> bool:
    return _is_invalid_id(content_id)


def is_credential_expired(platform: str, content_id: str) -> bool:
    return _is_credential_expired(_rng(platform, "cred", content_id))


def is_rate_limited(platform: str, content_id: str) -> bool:
    return _is_rate_limited(_rng(platform, "rate", content_id))
