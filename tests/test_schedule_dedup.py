"""v1.6.8: 定时任务历史去重（scheduled_seen_posts）storage 层测试"""

from datetime import datetime, timedelta

import pytest

from src.vocabulary.storage import VocabStorage


@pytest.fixture
def storage():
    return VocabStorage()


async def test_first_run_no_duplicates(init_test_db, storage):
    """首次抓取：无历史，全部视为新帖子"""
    ids = ["p1", "p2", "p3"]
    since = (datetime.now() - timedelta(days=30)).isoformat()
    seen = await storage.filter_seen_posts(ids, "kw:测试", since)
    assert seen == set()
    await storage.record_seen_posts(ids, "kw:测试", "task1", datetime.now().isoformat())


async def test_second_run_detects_duplicates(init_test_db, storage):
    """第二次抓取到相同 post_id：应被识别为重复"""
    since = (datetime.now() - timedelta(days=30)).isoformat()
    now = datetime.now().isoformat()
    await storage.record_seen_posts(["p1", "p2"], "kw:测试", "task1", now)

    seen = await storage.filter_seen_posts(["p1", "p3"], "kw:测试", since)
    assert seen == {"p1"}


async def test_dimension_isolation(init_test_db, storage):
    """不同维度（不同关键词）相互独立，相同 post_id 不算重复"""
    since = (datetime.now() - timedelta(days=30)).isoformat()
    now = datetime.now().isoformat()
    await storage.record_seen_posts(["p1"], "kw:关键词A", "t", now)

    seen = await storage.filter_seen_posts(["p1"], "kw:关键词B", since)
    assert seen == set()


async def test_expired_records_purged(init_test_db, storage):
    """超出时间窗的历史记录应被清理，允许重新抓取"""
    old_time = (datetime.now() - timedelta(days=40)).isoformat()
    await storage.record_seen_posts(["p1"], "kw:测试", "t", old_time)

    since = (datetime.now() - timedelta(days=30)).isoformat()
    seen = await storage.filter_seen_posts(["p1"], "kw:测试", since)
    assert seen == set()
