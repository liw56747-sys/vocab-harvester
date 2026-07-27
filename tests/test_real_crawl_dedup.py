"""v1.7.3: real_crawler 去重 + 严格迭代补足 + 评论单独成行 单测（无网络，用假抓取器）"""

import json

import pytest

from src.crawlers.real_crawler import _collect_new_twitter


def _mk_tweet(i: int) -> dict:
    """构造一条带 1 条回复的推文 dict。"""
    return {
        "tweet_id": f"t{i}", "content": f"post{i}", "author": f"u{i}",
        "created_at": "2026-07-01T08:00:00", "likes": 1, "retweets": 0, "replies": 1,
        "url": f"https://x.com/u{i}/status/t{i}",
        "replies_data": json.dumps([
            {"tweet_id": f"t{i}c1", "author": f"c{i}", "content": f"reply{i}",
             "created_at": "2026-07-01T08:01:00"}
        ]),
    }


class _FakeTW:
    """假 Twitter 抓取器：search_tweets 从固定池返回前 count 条。"""

    def __init__(self, pool_size: int):
        self._pool = [_mk_tweet(i) for i in range(1, pool_size + 1)]
        self.calls = 0

    async def search_tweets(self, kw, count, include_replies, cookies, sort_by):
        self.calls += 1
        return self._pool[:count], ""


def _split(collected):
    posts = [p for p in collected if (p.raw_data or {}).get("type") == "post"]
    comments = [p for p in collected if (p.raw_data or {}).get("type") == "comment"]
    return posts, comments


async def test_dedup_and_backfill_reaches_count():
    """历史重复被丢弃，且迭代补足到 count。"""
    fetcher = _FakeTW(pool_size=6)
    seen = {"t1", "t2"}  # 前两条为历史重复
    collected = await _collect_new_twitter(
        fetcher, "kw", count=3, cookies={}, sort_by="top",
        include_replies=True, seen_ids=seen,
    )
    posts, comments = _split(collected)
    # 补足到 3 条全新帖子（t3,t4,t5），不含历史重复
    assert [p.post_id for p in posts] == ["t3", "t4", "t5"]
    assert seen.isdisjoint({p.post_id for p in posts})
    # 每条帖子都带 1 条评论，且 parent_id 指向所属帖子
    assert len(comments) == 3
    for c in comments:
        assert c.raw_data["type"] == "comment"
        assert c.raw_data["parent_id"] in {"t3", "t4", "t5"}
    assert fetcher.calls >= 2  # 触发了补足重抓


async def test_stop_when_no_more_new():
    """没有更多新结果时直接输出当前结果（不无限重试）。"""
    fetcher = _FakeTW(pool_size=3)
    seen = {"t1", "t2"}
    collected = await _collect_new_twitter(
        fetcher, "kw", count=3, cookies={}, sort_by="top",
        include_replies=False, seen_ids=seen,
    )
    posts, comments = _split(collected)
    # 池中仅剩 t3 为新，补不满 3 条 → 输出 1 条
    assert [p.post_id for p in posts] == ["t3"]
    assert comments == []  # 未开启评论


async def test_no_dedup_returns_count():
    """无历史重复时，直接返回 count 条。"""
    fetcher = _FakeTW(pool_size=10)
    collected = await _collect_new_twitter(
        fetcher, "kw", count=5, cookies={}, sort_by="top",
        include_replies=False, seen_ids=set(),
    )
    posts, _ = _split(collected)
    assert len(posts) == 5
    assert fetcher.calls == 1  # 一轮即达标，无需补足
