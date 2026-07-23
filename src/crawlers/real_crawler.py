"""真实抓取封装：将 Cookie 抓取器（Twitter/Reddit）的结果映射为统一的 ParsedPost。

供定时任务在后台线程中调用（Playwright 需独立事件循环），返回 ParsedPost 列表，
再交给 Pipeline 做去重/分析/落盘。
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.common.models import ParsedPost

logger = logging.getLogger(__name__)


def friendly_error(exc) -> str:
    """将抓取异常映射为面向用户的友好提示。"""
    msg = str(exc or "").strip()
    low = msg.lower()
    if ("cookie" in low) or ("登录" in msg) or ("login" in low) or ("flow" in low) or ("logout" in low):
        return "Cookie 已过期或无效，请重新从浏览器导出并保存 Cookie"
    if ("429" in msg) or ("频繁" in msg) or ("rate limit" in low) or ("too many" in low):
        return "请求过于频繁（被限流），请稍后再试"
    if ("代理" in msg) or ("proxy" in low) or ("无法连接" in msg) or ("connect" in low)\
            or ("timeout" in low) or ("超时" in msg) or ("err_" in low) or ("net::" in low):
        return "网络/代理异常，请检查代理设置与网络连接"
    if ("something went wrong" in low) or ("出了点问题" in msg):
        return "平台页面异常，请稍后重试"
    return msg[:120] if msg else "未知错误"


class PrefetchedCrawler:
    """将已抓取好的 ParsedPost 列表包装为 Pipeline 可用的爬虫。

    真实抓取在后台线程（独立事件循环）完成后，用本类把结果交给
    Pipeline.run 的采集阶段，从而复用去重/分析/落盘逻辑（避免 Playwright 质主线程）。
    """

    def __init__(self, platform: str, posts: list[ParsedPost]):
        self.platform = platform
        self._posts = posts or []

    async def fetch(self, query) -> list[ParsedPost]:
        return self._posts


def _parse_dt(value) -> datetime:
    """尽力将各种时间格式解析为 datetime；失败则回退当前时间。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return datetime.now()
    s = str(value).strip()
    # 常见 ISO 格式
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            if fmt is None:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    return datetime.now()


def _tweet_to_post(t: dict, keyword: str) -> ParsedPost:
    """Twitter 抓取结果 dict → ParsedPost。"""
    return ParsedPost(
        platform="twitter",
        post_id=str(t.get("tweet_id") or t.get("id") or ""),
        content=t.get("content", "") or "",
        author=t.get("author") or t.get("author_name", "") or "",
        published_at=_parse_dt(t.get("created_at")),
        metrics={
            "likes": t.get("likes", 0) or 0,
            "retweets": t.get("retweets", 0) or 0,
            "replies": t.get("replies", 0) or 0,
        },
        tags=[keyword] if keyword else [],
        raw_data={"source": "twitter", "url": t.get("url", "")},
    )


def _reddit_to_post(d: dict, keyword: str) -> ParsedPost:
    """Reddit 抓取结果 dict → ParsedPost。"""
    title = d.get("title", "") or ""
    body = d.get("content", "") or ""
    content = (title + "\n" + body).strip() if title else body
    return ParsedPost(
        platform="reddit",
        post_id=str(d.get("post_id") or d.get("id") or ""),
        content=content,
        author=d.get("author") or d.get("commenter", "") or "",
        published_at=_parse_dt(d.get("created_at")),
        metrics={
            "score": d.get("score", 0) or 0,
            "num_comments": d.get("num_comments", 0) or 0,
        },
        tags=[keyword] if keyword else [],
        raw_data={"source": "reddit", "subreddit": d.get("subreddit", ""), "url": d.get("url", "")},
    )


async def crawl_twitter(
    keywords: list[str], count: int, cookies: dict, proxy: str | None,
    sort_by: str = "top", include_replies: bool = False, block_resources: bool = False,
) -> list[ParsedPost]:
    """对每个关键词调用 Twitter 搜索，聚合为 ParsedPost 列表。"""
    from src.crawlers.twitter_url import TwitterCookieFetcher

    fetcher = TwitterCookieFetcher(proxy=proxy, block_resources=block_resources)
    posts: list[ParsedPost] = []
    last_error = None
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        try:
            tweets, _ = await fetcher.search_tweets(
                kw, count=count, include_replies=include_replies,
                cookies=cookies, sort_by=sort_by,
            )
            posts.extend(_tweet_to_post(t, kw) for t in tweets)
            logger.info(f"[真实抓取] Twitter「{kw}」获取 {len(tweets)} 条")
        except Exception as e:
            last_error = e
            logger.error(f"[真实抓取] Twitter「{kw}」失败: {e}")
    # 若未抓到任何数据且存在错误，抛出原因供上层记录/展示
    if not posts and last_error is not None:
        raise last_error
    return posts


async def crawl_reddit(
    keywords: list[str], count: int, cookies: dict, proxy: str | None,
    sort_by: str = "top", include_replies: bool = False,
) -> list[ParsedPost]:
    """对每个关键词调用 Reddit 搜索，聚合为 ParsedPost 列表。"""
    from src.crawlers.reddit_crawler import RedditCookieFetcher

    fetcher = RedditCookieFetcher(proxy=proxy)
    sort = "hot" if sort_by == "top" else "new"
    posts: list[ParsedPost] = []
    last_error = None
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        try:
            items, _ = await fetcher.search_posts(
                kw, count=count, cookies=cookies, sort=sort, include_replies=include_replies,
            )
            posts.extend(_reddit_to_post(d, kw) for d in items)
            logger.info(f"[真实抓取] Reddit「{kw}」获取 {len(items)} 条")
        except Exception as e:
            last_error = e
            logger.error(f"[真实抓取] Reddit「{kw}」失败: {e}")
    if not posts and last_error is not None:
        raise last_error
    return posts
