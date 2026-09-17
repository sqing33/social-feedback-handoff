"""受限的小红书公开页面 Bridge。

该包只允许读取公开页面状态和主页作品卡片摘要；不处理 Cookie、请求头、
响应体、临时访问参数、文件上传或任意 JavaScript。
"""

from .client import SafeXhsBridgeClient, SafeBridgeError
from .protocol import SAFE_METHODS, validate_bridge_url

__all__ = [
    "SAFE_METHODS",
    "SafeBridgeError",
    "SafeXhsBridgeClient",
    "validate_bridge_url",
]
