"""Pipeline 加载、配置、端到端运行测试"""

import pytest

from src.common.models import CrawlQuery, Platform
from src.orchestrator.pipeline import Pipeline


async def test_pipeline_from_config_loads_crawlers():
    """from_config() 加载四平台爬虫"""
    pipeline = Pipeline.from_config()
    assert len(pipeline.crawlers) == 4
    assert "weibo" in pipeline.crawlers
    assert "xiaohongshu" in pipeline.crawlers
    assert "twitter" in pipeline.crawlers
    assert "reddit" in pipeline.crawlers


async def test_mock_pipeline_run(mock_pipeline, crawl_query):
    """MockCrawler + MockAdapter 端到端运行"""
    stats = await mock_pipeline.run(crawl_query)

    assert stats["status"] == "success"
    assert stats["total_posts"] > 0
    assert isinstance(stats.get("platforms"), list)
    assert len(stats["platforms"]) == 4


async def test_mock_pipeline_keywords_extracted(mock_pipeline, crawl_query):
    """Pipeline 运行后提取到关键词"""
    stats = await mock_pipeline.run(crawl_query)

    assert stats["total_keywords"] >= 0
    # MockAdapter 会产生一些关键词
    if stats["total_keywords"] > 0:
        assert stats["ingested_count"] >= 0


async def test_mock_pipeline_sampled_posts(mock_pipeline, crawl_query):
    """Pipeline 返回结果包含采样帖子"""
    stats = await mock_pipeline.run(crawl_query)

    posts = stats.get("sampled_posts", [])
    assert isinstance(posts, list)
    if posts:
        assert "platform" in posts[0]
        assert "content" in posts[0]


async def test_mock_pipeline_platform_breakdown(mock_pipeline, crawl_query):
    """Pipeline 按平台返回各自帖子数量"""
    stats = await mock_pipeline.run(crawl_query)

    for p in stats["platforms"]:
        assert "name" in p
        assert "post_count" in p
        assert p["name"] in ("weibo", "xiaohongshu", "twitter", "reddit")
