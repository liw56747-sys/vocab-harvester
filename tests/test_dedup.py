import pytest
import sys
import os

# 将项目根目录添加到Python路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.vocabulary.manager import VocabManager
from src.common.database import get_db
from datetime import datetime, timedelta


async def test_dedup_by_keyword():
    # 1. 用关键词A抓取5条数据
    manager = VocabManager()
    posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "keywords": "A"}
        for i in range(1, 6)
    ]
    manager.ingest(None, posts, task_name="test")

    # 2. 重新用关键词A抓取相同数据
    duplicate_posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "keywords": "A"}
        for i in range(1, 6)
    ]
    result = manager.ingest(None, duplicate_posts, task_name="test")
    assert result == 0  # 应全部被去重


async def test_dedup_by_user():
    # 1. 用用户ID 1抓取5条数据
    manager = VocabManager()
    posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "user_id": "1"}
        for i in range(1, 6)
    ]
    manager.ingest(None, posts, task_name="test")

    # 2. 重新用用户ID 1抓取相同数据
    duplicate_posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "user_id": "1"}
        for i in range(1, 6)
    ]
    result = manager.ingest(None, duplicate_posts, task_name="test")
    assert result == 0  # 应全部被去重


async def test_allow_cross_keyword():
    # 1. 用关键词A抓取5条数据
    manager = VocabManager()
    posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "keywords": "A"}
        for i in range(1, 6)
    ]
    manager.ingest(None, posts, task_name="test")

    # 2. 用关键词B抓取相同数据
    cross_posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "keywords": "B"}
        for i in range(1, 6)
    ]
    result = manager.ingest(None, cross_posts, task_name="test")
    assert result == 5  # 应全部保留


async def test_retention_period():
    # 1. 用关键词A抓取5条数据
    manager = VocabManager()
    posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "keywords": "A"}
        for i in range(1, 6)
    ]
    manager.ingest(None, posts, task_name="test")

    # 2. 模拟30天后
    async with get_db() as conn:
        conn.execute("UPDATE posts SET created_at = datetime('now', '-31 days')")

    # 3. 重新抓取相同数据
    duplicate_posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "keywords": "A"}
        for i in range(1, 6)
    ]
    result = manager.ingest(None, duplicate_posts, task_name="test")
    assert result == 5  # 30天后应重新抓取

    # 4. 用用户ID 1抓取5条数据
    posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "user_id": "1"}
        for i in range(1, 6)
    ]
    manager.ingest(None, posts, task_name="test")

    # 5. 模拟90天后
    async with get_db() as conn:
        conn.execute("UPDATE posts SET created_at = datetime('now', '-91 days')")

    # 6. 重新抓取相同数据
    duplicate_posts = [
        {"content": f"test {i}", "url": f"https://example.com/{i}", "user_id": "1"}
        for i in range(1, 6)
    ]
    result = manager.ingest(None, duplicate_posts, task_name="test")
    assert result == 5  # 90天后应重新抓取