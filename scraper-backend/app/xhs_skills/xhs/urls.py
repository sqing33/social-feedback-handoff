"""小红书 URL 常量和构建函数。"""

from urllib.parse import urlencode, quote

# 基础页面
EXPLORE_URL = "https://www.xiaohongshu.com/explore"
HOME_URL = "https://www.xiaohongshu.com"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

# Default xsec_source for feed detail URLs built from search-derived tokens.
# xsec_token is bound to the source it was issued with: a token from search
# results must be paired with pc_search, otherwise the server rejects with a
# 302→/404. Search is the dominant path in this project, so pc_search is the
# safe default. pc_feed is only correct for tokens issued from the home feed.
DEFAULT_FEED_SOURCE = "pc_search"


def make_feed_detail_url(feed_id: str, xsec_token: str, source: str = "") -> str:
    """构建 feed 详情页 URL。

    ``source`` maps to the ``xsec_source`` query parameter and must match the
    scope the token was issued under (``pc_search`` for search results,
    ``pc_feed`` for home-feed clicks). Default is ``pc_search`` — see
    :data:`DEFAULT_FEED_SOURCE`.

    Some note ids carry a ``#<sub_id>`` fragment (e.g. video / long-image
    notes like ``ee24d8c6-...#1785779642854``). The ``#`` is the URL
    fragment delimiter: anything after it (including xsec_token) is never
    sent to the server, causing a 风控 302→404 ("URL 缺少 xsec_token").
    We strip the fragment from the path and append it *after* the query
    string so the token stays in the server-visible portion.
    """
    xsec_source = source or DEFAULT_FEED_SOURCE
    path_id = feed_id
    fragment = ""
    if "#" in feed_id:
        path_id, fragment = feed_id.split("#", 1)
    base = (
        f"https://www.xiaohongshu.com/explore/{quote(path_id, safe='')}"
        f"?xsec_token={quote(xsec_token, safe='')}&xsec_source={xsec_source}"
    )
    if fragment:
        base += f"#{fragment}"
    return base


def make_search_url(keyword: str) -> str:
    """构建搜索结果页 URL。"""
    params = urlencode({"keyword": keyword, "source": "web_explore_feed"})
    return f"https://www.xiaohongshu.com/search_result?{params}"


def make_user_profile_url(user_id: str, xsec_token: str) -> str:
    """构建用户主页 URL。"""
    return (
        f"https://www.xiaohongshu.com/user/profile/{user_id}"
        f"?xsec_token={xsec_token}&xsec_source=pc_note"
    )
