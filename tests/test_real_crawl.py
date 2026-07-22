"""v1.6.9: 定时任务真实抓取 —— ParsedPost 映射 + 平台 Cookie 服务端持久化测试"""

from datetime import datetime

import pytest

from src.crawlers.real_crawler import _tweet_to_post, _reddit_to_post, _parse_dt, PrefetchedCrawler


# ── dict → ParsedPost 映射 ──

def test_tweet_to_post_mapping():
    t = {
        "tweet_id": "123", "author": "alice", "content": "hello",
        "created_at": "2026-07-01T08:00:00", "likes": 10, "retweets": 2, "replies": 1,
        "url": "https://x.com/alice/123",
    }
    p = _tweet_to_post(t, "测试")
    assert p.platform == "twitter"
    assert p.post_id == "123"
    assert p.author == "alice"
    assert p.content == "hello"
    assert p.metrics["likes"] == 10 and p.metrics["retweets"] == 2 and p.metrics["replies"] == 1
    assert p.tags == ["测试"]
    assert isinstance(p.published_at, datetime)


def test_reddit_to_post_mapping():
    d = {
        "post_id": "r1", "author": "bob", "title": "标题", "content": "正文",
        "created_at": "2026-07-01 09:00:00", "score": 5, "num_comments": 3,
        "subreddit": "news",
    }
    p = _reddit_to_post(d, "keyword")
    assert p.platform == "reddit"
    assert p.post_id == "r1"
    assert p.author == "bob"
    assert "标题" in p.content and "正文" in p.content
    assert p.metrics["score"] == 5 and p.metrics["num_comments"] == 3
    assert p.tags == ["keyword"]


def test_parse_dt_fallbacks():
    # 合法 ISO
    assert _parse_dt("2026-07-01T08:00:00").year == 2026
    # 合法空格格式
    assert _parse_dt("2026-07-01 08:00:00").year == 2026
    # 非法 → 回退当前时间（不抛异常）
    assert isinstance(_parse_dt("not-a-date"), datetime)
    assert isinstance(_parse_dt(None), datetime)


async def test_prefetched_crawler_returns_posts():
    posts = [_tweet_to_post({"tweet_id": "1", "content": "x"}, "k")]
    crawler = PrefetchedCrawler("twitter", posts)
    result = await crawler.fetch(query=None)
    assert result == posts


# ── 平台 Cookie 服务端持久化 ──

async def test_platform_cookie_persistence(init_test_db):
    from src.api.main import _save_platform_cookies_db, _get_platform_cookies_db

    await _save_platform_cookies_db("twitter", {"ct0": "abc", "auth_token": "xyz", "proxy": ""})
    await _save_platform_cookies_db("reddit", {"reddit_session": "sess", "proxy": "http://127.0.0.1:7890"})

    data = await _get_platform_cookies_db()
    assert data["twitter"]["ct0"] == "abc"
    assert data["twitter"]["auth_token"] == "xyz"
    assert data["reddit"]["reddit_session"] == "sess"
    assert data["reddit"]["proxy"] == "http://127.0.0.1:7890"


async def test_platform_cookie_upsert(init_test_db):
    """重复保存同一平台应更新而非新增"""
    from src.api.main import _save_platform_cookies_db, _get_platform_cookies_db

    await _save_platform_cookies_db("twitter", {"ct0": "old", "auth_token": "t1"})
    await _save_platform_cookies_db("twitter", {"ct0": "new", "auth_token": "t2"})
    data = await _get_platform_cookies_db()
    assert data["twitter"]["ct0"] == "new"
    assert len(data) == 1
