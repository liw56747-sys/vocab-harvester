"""MockCrawler 各平台抓取测试"""

import pytest

from src.common.models import CrawlQuery, Platform
from src.crawlers.mock import MockCrawler


ALL_PLATFORMS = [Platform.WEIBO, Platform.XIAOHONGSHU, Platform.TWITTER]


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
async def test_mock_crawler_returns_posts(platform):
    """每个平台的 MockCrawler 都能返回帖子"""
    query = CrawlQuery(platforms=[platform], keywords=["避坑军校"], max_results=10)
    crawler = MockCrawler(platform)
    posts = await crawler.fetch(query)

    assert len(posts) > 0
    assert len(posts) <= 10


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
async def test_mock_crawler_post_fields(platform):
    """返回的帖子包含必要字段"""
    query = CrawlQuery(platforms=[platform], keywords=["避坑军校"], max_results=10)
    crawler = MockCrawler(platform)
    posts = await crawler.fetch(query)

    first = posts[0]
    assert first.platform == platform.value
    assert first.content
    assert first.author
    assert first.post_id


async def test_mock_crawler_all_platforms_total():
    """三平台 MockCrawler 合计返回合理数量"""
    query = CrawlQuery(
        platforms=ALL_PLATFORMS, keywords=["避坑军校"], max_results=10
    )
    total = 0
    for p in ALL_PLATFORMS:
        crawler = MockCrawler(p)
        posts = await crawler.fetch(query)
        total += len(posts)

    assert total > 0
    assert total <= 30  # 3 platforms * 10 max_results
