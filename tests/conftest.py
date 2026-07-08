"""pytest 共享 fixture：测试数据库初始化、Pipeline 构建、通用查询"""

import asyncio
import tempfile
import os
from pathlib import Path

import pytest

from src.common.database import init_db, close_db
from src.common.models import CrawlQuery, Platform
from src.crawlers.mock import MockCrawler
from src.adapter.mock import MockAdapter
from src.orchestrator.pipeline import Pipeline


@pytest.fixture
async def init_test_db():
    """用临时 SQLite 文件初始化数据库，测试结束后自动清理"""
    import src.common.database as db_module

    # 确保上一次测试的连接已关闭
    if db_module._connection is not None:
        await close_db()

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()

    await init_db(db_path)
    yield db_path

    await close_db()
    # 双重保险：强制重置全局连接状态
    db_module._connection = None
    db_module._db_path = None

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def crawl_query():
    """标准四平台查询"""
    return CrawlQuery(
        platforms=[Platform.WEIBO, Platform.XIAOHONGSHU, Platform.TWITTER, Platform.REDDIT],
        keywords=["避坑军校"],
        max_results=10,
    )


@pytest.fixture
async def mock_pipeline(init_test_db):
    """MockCrawler + MockAdapter 构造的完整 Pipeline（数据库已初始化）"""
    crawlers = {
        "weibo": MockCrawler(Platform.WEIBO),
        "xiaohongshu": MockCrawler(Platform.XIAOHONGSHU),
        "twitter": MockCrawler(Platform.TWITTER),
        "reddit": MockCrawler(Platform.REDDIT),
    }
    return Pipeline(crawlers=crawlers, adapter=MockAdapter())
