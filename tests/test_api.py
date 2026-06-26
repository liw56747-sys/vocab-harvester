"""HTTP API 集成测试（需要运行中的服务器）

运行方式: python -m pytest tests/test_api.py -v -m integration
"""

import json
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=120.0) as c:
        yield c


async def test_server_health(client):
    """服务器 /api/stats 可访问"""
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data


async def test_crawl_api(client):
    """POST /api/crawl 返回完整的采集结果"""
    resp = await client.post(
        "/api/crawl",
        json={
            "platforms": ["weibo", "xiaohongshu", "twitter"],
            "keywords": ["避坑军校"],
            "max_results": 10,
        },
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data.get("status") in ("success", "completed")
    assert data.get("total_posts", 0) > 0
    assert isinstance(data.get("platforms"), list)


async def test_import_api(client):
    """POST /api/import 文件导入并处理"""
    fixture = Path(__file__).parent / "fixtures" / "test_data.json"
    if not fixture.exists():
        pytest.skip("test_data.json 不存在")

    with open(fixture, "rb") as f:
        resp = await client.post(
            "/api/import?platform=xiaohongshu",
            files={"file": ("test_data.json", f, "application/json")},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") in ("completed", "success")
    assert data.get("total_posts", 0) > 0
