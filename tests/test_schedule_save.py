"""v1.6.6: 定时任务「仅抓取数据」结果落盘测试（CSV / xlsx）"""

import csv
import io
import os

import pytest
from openpyxl import load_workbook

from src.api.main import save_task_results_to_file


def _sample_stats():
    """模拟仅抓取模式 pipeline 返回的 stats（含完整 crawled_posts）"""
    return {
        "status": "success",
        "analyze": False,
        "total_posts": 2,
        "total_keywords": 0,
        "platforms": [
            {"name": "twitter", "post_count": 1},
            {"name": "reddit", "post_count": 1},
        ],
        "crawled_posts": [
            {
                "platform": "twitter", "post_id": "t1", "author": "alice",
                "content": "hello world", "published_at": "2026-07-01T08:00:00",
                "likes": 10, "retweets": 2, "replies": 1, "tags": "a,b",
            },
            {
                "platform": "reddit", "post_id": "r1", "author": "bob",
                "content": "foo bar", "published_at": "2026-07-01T09:00:00",
                "likes": 5, "retweets": 0, "replies": 3, "tags": "",
            },
        ],
    }


async def test_save_crawl_only_csv(tmp_path):
    """仅抓取模式：CSV 完整写入抓取到的帖子明细"""
    path = await save_task_results_to_file(
        save_path=str(tmp_path), task_name="测试任务",
        stats=_sample_stats(), results=None, file_format="csv",
    )
    assert path is not None
    assert path.endswith(".csv")
    assert os.path.exists(path)

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    # 首行表头 + 2 条数据
    assert rows[0] == ["platform", "post_id", "author", "content", "published_at",
                       "likes", "retweets", "replies", "tags", "duplicate"]
    assert len(rows) == 3
    assert rows[1][0] == "twitter" and rows[1][2] == "alice"
    assert rows[2][0] == "reddit" and rows[2][3] == "foo bar"


async def test_save_crawl_only_xlsx(tmp_path):
    """仅抓取模式：xlsx 完整写入抓取到的帖子明细"""
    path = await save_task_results_to_file(
        save_path=str(tmp_path), task_name="测试任务",
        stats=_sample_stats(), results=None, file_format="xlsx",
    )
    assert path is not None
    assert path.endswith(".xlsx")

    wb = load_workbook(path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header[0] == "platform" and "content" in header
    # 表头 + 2 行数据
    assert ws.max_row == 3


async def test_save_uses_sampled_posts_fallback(tmp_path):
    """无 crawled_posts 时回退使用 sampled_posts（分析模式也能落盘帖子）"""
    stats = {
        "status": "success",
        "sampled_posts": [
            {"platform": "twitter", "post_id": "t1", "author": "alice",
             "content": "hi", "published_at": "2026-07-01T08:00:00",
             "metrics": {"likes": 3, "retweets": 1, "replies": 0}, "tags": []},
        ],
    }
    path = await save_task_results_to_file(
        save_path=str(tmp_path), task_name="t", stats=stats, file_format="csv",
    )
    assert path is not None
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 2          # 表头 + 1 条
    assert rows[1][5] == "3"       # metrics.likes 展平到 likes 列


async def test_save_no_path_returns_none():
    """未指定保存路径时返回 None（不落盘）"""
    path = await save_task_results_to_file(
        save_path="", task_name="t", stats=_sample_stats(), file_format="csv",
    )
    assert path is None
