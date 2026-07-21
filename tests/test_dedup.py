"""VocabManager 来源帖子去重逻辑单元测试。

注意：本测试只验证单次去重调用内的内存去重行为（_dedup_posts）。
黑词持久化（ingest 写入 vocab.db）由其他测试覆盖。
"""

import pytest
import sys
import os
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.vocabulary.manager import VocabManager
from src.common.models import ParsedPost


def _make_post(i, *, post_id=None, content=None, platform="test", **extra):
    """构建一个测试用的 ParsedPost 对象。"""
    return ParsedPost(
        platform=platform,
        post_id=post_id or f"https://example.com/{i}",
        content=content or f"test {i}",
        author="author",
        published_at=datetime.now(),
        raw_data=extra,
    )


def test_dedup_by_post_id():
    """相同 post_id 在单次去重内应只保留一条。"""
    manager = VocabManager()
    posts = [
        _make_post(i, post_id="same-id", content=f"same content {i}")
        for i in range(5)
    ]
    assert len(manager._dedup_posts(posts)) == 1


def test_dedup_by_content_similarity():
    """内容高度相似（Jaccard > 0.7）的帖子应被去重。"""
    manager = VocabManager()
    posts = [
        _make_post(i, content="this is almost the same post every time")
        for i in range(5)
    ]
    assert len(manager._dedup_posts(posts)) == 1


def test_keep_different_content():
    """内容差异较大的帖子应全部保留。"""
    manager = VocabManager()
    posts = [_make_post(i, content=f"completely different content {i}") for i in range(5)]
    assert len(manager._dedup_posts(posts)) == 5
