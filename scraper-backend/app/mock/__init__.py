"""Mock 数据生成器。

实现策略：
- 所有平台抓取都不真正访问外部网络（避免法律与稳定性问题）
- 基于 platform + content_id 做 hash 生成确定性伪数据
- 同一组输入多次请求返回相同结果，方便联调与测试
- 错误码路径（INVALID_CONTENT_ID、CONTENT_NOT_FOUND 等）按规则触发
"""

from .data import (
    PLATFORMS,
    generate_comments,
    generate_note_info,
    generate_search_results,
    generate_user_videos,
    is_credential_expired,
    is_invalid_content_id,
    is_rate_limited,
)

__all__ = [
    "PLATFORMS",
    "generate_comments",
    "generate_note_info",
    "generate_search_results",
    "generate_user_videos",
    "is_credential_expired",
    "is_invalid_content_id",
    "is_rate_limited",
]
