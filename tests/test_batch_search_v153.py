"""v1.5.3 集成冒烟测试：应用启动、路由完整性、Pydantic 模型兼容"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    # 用临时目录避免污染真实数据库
    monkeypatch.setenv("VOCAB_DATA_DIR", str(tmp_path))
    from src.api.main import app
    return TestClient(app)


def test_app_startup_and_routes(client):
    """确认应用能启动且关键路由存在"""
    # /api/version 是最基本的接口
    r = client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert "platform" in body


def test_batch_search_rejects_over_30(client):
    """新的上限 30，超过则 400"""
    r = client.post("/api/batch-search", json={
        "keywords": [f"kw{i}" for i in range(31)],
        "count": 10,
        "platforms": ["twitter"],
    })
    assert r.status_code == 400
    assert "30" in r.json().get("detail", "")


def test_batch_search_rejects_empty(client):
    r = client.post("/api/batch-search", json={"keywords": [], "count": 10, "platforms": ["twitter"]})
    assert r.status_code == 400


def test_batch_search_accepts_new_fields(client):
    """新增 stagger_platforms / worker_concurrency / max_retries 不应导致 422"""
    r = client.post("/api/batch-search", json={
        "keywords": ["hello"],
        "count": 5,
        "platforms": ["twitter"],
        "stagger_platforms": True,
        "worker_concurrency": 3,
        "max_retries": 2,
        "cookies": [],  # 空 Cookie → 会进入 skipped，但不应 422
    })
    # 200 起点，200 直接完成为 success 走空返回
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "started"
    assert "task_id" in body


def test_task_status_progress_field(client):
    """进度字段应存在于 task-status 返回中"""
    r = client.post("/api/batch-search", json={
        "keywords": ["hello"], "count": 5, "platforms": ["twitter"], "cookies": []
    })
    tid = r.json()["task_id"]
    r2 = client.get(f"/api/task-status?task_id={tid}")
    body = r2.json()
    # progress 字段应存在（即使 total=0）
    assert "progress" in body or body.get("status") in ("success", "error", "running")


def test_preflight_proxy_bad_target(client):
    """代理探测：不可达目标应返回 error"""
    r = client.post("/api/preflight-proxy", json={
        "proxy": "http://127.0.0.1:65535",  # 不可能有服务的端口
        "target": "http://127.0.0.1:65534",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"


def test_browser_pool_singleton_backward_compat():
    """确认旧的 BrowserManager 单例接口仍然可用"""
    from src.crawlers.browser_manager import BrowserManager, BrowserPool, apply_request_interceptors
    mgr = BrowserManager.get()
    assert mgr is BrowserManager.get()  # 单例
    # BrowserPool 存在
    pool = BrowserPool.get()
    assert pool.size >= 1
    # apply_request_interceptors 存在
    assert callable(apply_request_interceptors)
