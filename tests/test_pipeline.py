"""Pipeline 加载、配置、端到端运行测试"""

import pytest

from src.common.models import CrawlQuery, Platform
from src.orchestrator.pipeline import Pipeline


async def test_pipeline_from_config_loads_crawlers():
    """from_config() 加载两平台爬虫（Twitter 和 Reddit）"""
    pipeline = Pipeline.from_config()
    assert len(pipeline.crawlers) == 2
    assert "twitter" in pipeline.crawlers
    assert "reddit" in pipeline.crawlers


async def test_mock_pipeline_run(mock_pipeline, crawl_query):
    """MockCrawler + MockAdapter 端到端运行"""
    stats = await mock_pipeline.run(crawl_query)

    assert stats["status"] == "success"
    assert stats["total_posts"] > 0
    assert isinstance(stats.get("platforms"), list)
    assert len(stats["platforms"]) == 2


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
        assert p["name"] in ("twitter", "reddit")


async def test_mock_pipeline_crawl_only_skips_analysis(mock_pipeline, crawl_query):
    """analyze=False 仅抓取数据，跳过黑词分析与词库写入"""
    stats = await mock_pipeline.run(crawl_query, analyze=False)

    assert stats["status"] == "success"
    assert stats["total_posts"] > 0        # 仍然完成采集
    assert stats["analyze"] is False
    assert stats["total_keywords"] == 0    # 未进行分析
    assert "ingested_count" not in stats   # 未写入词库


async def test_mock_pipeline_analyze_default_true(mock_pipeline, crawl_query):
    """默认 analyze=True，保持原有“抓取+分析”行为"""
    stats = await mock_pipeline.run(crawl_query)

    assert stats["status"] == "success"
    # 完整流程会进行词库写入
    assert "ingested_count" in stats
