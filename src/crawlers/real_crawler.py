"""真实抓取封装：将 Cookie 抓取器（Twitter/Reddit）的结果映射为统一的 ParsedPost。

供定时任务在后台线程中调用（Playwright 需独立事件循环），返回 ParsedPost 列表，
再交给 Pipeline 做分析/落盘。

v1.7.3：
  - 评论作为独立 ParsedPost 输出（raw_data.type="comment"，parent_id 指向所属帖子），
    评论紧跟所属帖子，便于导出成"单独成行"。
  - 历史去重下沉到抓取层：入参 seen_ids（已抓过的帖子 id）用于丢弃历史重复，
    并"严格迭代补足"到设定条数；平台无更多新结果时直接输出当前结果。
"""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime

from src.common.models import ParsedPost

logger = logging.getLogger(__name__)

# 严格迭代补足的兜底上限：最多重试轮数 / 单关键词绝对抓取上限
_BACKFILL_MAX_ROUNDS = 4
_BACKFILL_ABS_CAP = 300


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
    Pipeline.run 的采集阶段，从而复用分析/落盘逻辑（避免 Playwright 上主线程）。
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
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            if fmt is None:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    return datetime.now()


def _parse_replies(raw) -> list[dict]:
    """将推文的 replies_data（JSON 字符串或列表）解析为 dict 列表。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = _json.loads(raw)
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


# ── dict → ParsedPost 映射 ────────────────────────────────

def _tweet_to_post(t: dict, keyword: str) -> ParsedPost:
    """Twitter 帖子 dict → ParsedPost。"""
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
        raw_data={"source": "twitter", "type": "post", "parent_id": "", "url": t.get("url", "")},
    )


def _tweet_reply_to_post(r: dict, parent_id: str, keyword: str) -> ParsedPost:
    """Twitter 回复 dict → 评论 ParsedPost。"""
    return ParsedPost(
        platform="twitter",
        post_id=str(r.get("tweet_id") or ""),
        content=r.get("content", "") or "",
        author=r.get("author") or r.get("display_name", "") or "",
        published_at=_parse_dt(r.get("created_at")),
        metrics={},
        tags=[keyword] if keyword else [],
        raw_data={"source": "twitter", "type": "comment", "parent_id": parent_id, "url": ""},
    )


def _reddit_to_post(d: dict, keyword: str) -> ParsedPost:
    """Reddit 帖子 dict → ParsedPost。"""
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
        raw_data={"source": "reddit", "type": "post", "parent_id": "",
                  "subreddit": d.get("subreddit", ""), "url": d.get("url", "")},
    )


def _reddit_comment_to_post(c: dict, keyword: str) -> ParsedPost:
    """Reddit 评论 dict → 评论 ParsedPost（parent_id 指向所属帖子）。"""
    return ParsedPost(
        platform="reddit",
        post_id="",  # Reddit 评论行无独立 post_id，以 parent_id 关联所属帖子
        content=c.get("content", "") or "",
        author=c.get("commenter") or c.get("author", "") or "",
        published_at=_parse_dt(c.get("created_at")),
        metrics={"score": c.get("score", 0) or 0},
        tags=[keyword] if keyword else [],
        raw_data={"source": "reddit", "type": "comment",
                  "parent_id": str(c.get("post_id", "")), "url": c.get("url", "")},
    )


# ── 单关键词：去重 + 严格迭代补足到 count ──────────────────

async def _collect_new_twitter(fetcher, kw, count, cookies, sort_by, include_replies, seen_ids):
    """抓取单个关键词的 Twitter 新帖（去重 + 迭代补足），返回 [帖子, 其评论...] 交错列表。"""
    collected: list[ParsedPost] = []
    collected_ids: set[str] = set()
    target = count
    prev_fetched = -1
    for _round in range(_BACKFILL_MAX_ROUNDS):
        fetch_n = min(target, _BACKFILL_ABS_CAP)
        tweets, _ = await fetcher.search_tweets(
            kw, count=fetch_n, include_replies=include_replies,
            cookies=cookies, sort_by=sort_by,
        )
        added = 0
        for t in tweets:
            pid = str(t.get("tweet_id") or t.get("id") or "")
            if not pid or pid in seen_ids or pid in collected_ids:
                continue
            collected_ids.add(pid)
            collected.append(_tweet_to_post(t, kw))
            if include_replies:
                for r in _parse_replies(t.get("replies_data")):
                    collected.append(_tweet_reply_to_post(r, pid, kw))
            added += 1
            if len(collected_ids) >= count:
                break
        # 已达标 / 本轮无新增（平台无更多新结果）/ 返回条数不再增长（触底）→ 直接输出
        if len(collected_ids) >= count or added == 0 or len(tweets) <= prev_fetched:
            break
        prev_fetched = len(tweets)
        target = min(target * 2, _BACKFILL_ABS_CAP)
    logger.info(f"[真实抓取] Twitter「{kw}」新帖 {len(collected_ids)} 条（目标 {count}）")
    return collected


async def _collect_new_reddit(fetcher, kw, count, cookies, sort, include_replies, seen_ids):
    """抓取单个关键词的 Reddit 新帖（去重 + 迭代补足），返回 [帖子, 其评论...] 交错列表。"""
    collected: list[ParsedPost] = []
    collected_ids: set[str] = set()
    target = count
    prev_posts = -1
    for _round in range(_BACKFILL_MAX_ROUNDS):
        fetch_n = min(target, _BACKFILL_ABS_CAP)
        rows, _ = await fetcher.search_posts(
            kw, count=fetch_n, cookies=cookies, sort=sort, include_replies=include_replies,
        )
        posts = [r for r in rows if r.get("type") != "comment"]
        comments_by_parent: dict[str, list[dict]] = {}
        for r in rows:
            if r.get("type") == "comment":
                comments_by_parent.setdefault(str(r.get("post_id", "")), []).append(r)
        added = 0
        for d in posts:
            pid = str(d.get("post_id") or d.get("id") or "")
            if not pid or pid in seen_ids or pid in collected_ids:
                continue
            collected_ids.add(pid)
            collected.append(_reddit_to_post(d, kw))
            for c in comments_by_parent.get(pid, []):
                collected.append(_reddit_comment_to_post(c, kw))
            added += 1
            if len(collected_ids) >= count:
                break
        if len(collected_ids) >= count or added == 0 or len(posts) <= prev_posts:
            break
        prev_posts = len(posts)
        target = min(target * 2, _BACKFILL_ABS_CAP)
    logger.info(f"[真实抓取] Reddit「{kw}」新帖 {len(collected_ids)} 条（目标 {count}）")
    return collected


# ── 平台入口 ──────────────────────────────────────────────

async def crawl_twitter(
    keywords: list[str], count: int, cookies: dict, proxy: str | None,
    sort_by: str = "top", include_replies: bool = False, block_resources: bool = False,
    seen_ids: set[str] | None = None,
) -> list[ParsedPost]:
    """对每个关键词抓取 Twitter 新帖（去重+补足），聚合为 ParsedPost 列表（含评论行）。"""
    from src.crawlers.twitter_url import TwitterCookieFetcher

    seen_ids = seen_ids or set()
    fetcher = TwitterCookieFetcher(proxy=proxy, block_resources=block_resources)
    posts: list[ParsedPost] = []
    last_error = None
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        try:
            posts.extend(await _collect_new_twitter(
                fetcher, kw, count, cookies, sort_by, include_replies, seen_ids
            ))
        except Exception as e:
            last_error = e
            logger.error(f"[真实抓取] Twitter「{kw}」失败: {e}")
    if not posts and last_error is not None:
        raise last_error
    return posts


async def crawl_reddit(
    keywords: list[str], count: int, cookies: dict, proxy: str | None,
    sort_by: str = "top", include_replies: bool = False,
    seen_ids: set[str] | None = None,
) -> list[ParsedPost]:
    """对每个关键词抓取 Reddit 新帖（去重+补足），聚合为 ParsedPost 列表（含评论行）。"""
    from src.crawlers.reddit_crawler import RedditCookieFetcher

    seen_ids = seen_ids or set()
    fetcher = RedditCookieFetcher(proxy=proxy)
    sort = "hot" if sort_by == "top" else "new"
    posts: list[ParsedPost] = []
    last_error = None
    for kw in keywords:
        kw = (kw or "").strip()
        if not kw:
            continue
        try:
            posts.extend(await _collect_new_reddit(
                fetcher, kw, count, cookies, sort, include_replies, seen_ids
            ))
        except Exception as e:
            last_error = e
            logger.error(f"[真实抓取] Reddit「{kw}」失败: {e}")
    if not posts and last_error is not None:
        raise last_error
    return posts
