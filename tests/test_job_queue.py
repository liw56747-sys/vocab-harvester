"""job_queue 单元测试（纯 async，不依赖网络/浏览器）"""

import asyncio

import pytest

from src.orchestrator.job_queue import (
    KeywordJobQueue,
    JobStatus,
)


@pytest.mark.asyncio
async def test_all_success():
    q = KeywordJobQueue(concurrency=2)

    async def worker(kw, plat):
        await asyncio.sleep(0.01)
        return f"{kw}-{plat}-ok"

    for kw in ("apple", "banana", "cherry"):
        for plat in ("twitter", "reddit"):
            q.enqueue(kw, plat, worker=worker)

    await q.run()
    jobs = q.results()
    assert len(jobs) == 6
    assert all(j.status == JobStatus.SUCCESS for j in jobs)
    assert all(j.result and j.result.endswith("-ok") for j in jobs)
    snap = q.snapshot()
    assert snap.total == 6
    assert snap.completed == 6
    assert snap.failed == 0


@pytest.mark.asyncio
async def test_retry_and_final_failure():
    q = KeywordJobQueue(concurrency=1)
    calls = {"n": 0}

    async def worker(kw, plat):
        calls["n"] += 1
        raise RuntimeError("boom")

    q.enqueue("always-fail", "twitter", worker=worker, max_retries=2)

    # 覆盖退避为极短
    for j in q.results():
        j._next_backoff = 0.01

    await q.run()
    jobs = q.results()
    assert jobs[0].status == JobStatus.FAILED
    # max_retries=2 → 首次 + 2 次重试 = 3 次调用
    assert calls["n"] == 3
    assert jobs[0].retries == 2
    assert q.snapshot().failed == 1


@pytest.mark.asyncio
async def test_partial_failure_isolation():
    """一个 job 失败不应牵连其它 job"""
    q = KeywordJobQueue(concurrency=3)

    async def worker(kw, plat):
        if kw == "bad":
            raise ValueError("nope")
        return "ok"

    for kw in ("good1", "bad", "good2"):
        q.enqueue(kw, "twitter", worker=worker, max_retries=0)

    for j in q.results():
        j._next_backoff = 0.01

    await q.run()
    jobs = q.results()
    statuses = {j.keyword: j.status for j in jobs}
    assert statuses["good1"] == JobStatus.SUCCESS
    assert statuses["good2"] == JobStatus.SUCCESS
    assert statuses["bad"] == JobStatus.FAILED


@pytest.mark.asyncio
async def test_cancel_stops_pending():
    """取消后未启动的 job 应变为 CANCELLED"""
    cancel_flag = {"stop": False}
    q = KeywordJobQueue(concurrency=1, is_cancelled=lambda: cancel_flag["stop"])

    async def worker(kw, plat):
        await asyncio.sleep(0.02)
        return "ok"

    for kw in ("a", "b", "c", "d"):
        q.enqueue(kw, "twitter", worker=worker)

    async def _canceller():
        await asyncio.sleep(0.03)
        cancel_flag["stop"] = True

    await asyncio.gather(q.run(), _canceller())
    jobs = q.results()
    statuses = [j.status for j in jobs]
    # 至少后面几个是 CANCELLED
    assert JobStatus.CANCELLED in statuses


@pytest.mark.asyncio
async def test_platform_staggering():
    """platform_order 应先跑完 reddit 再跑 twitter"""
    events: list[str] = []
    q = KeywordJobQueue(concurrency=2, platform_order=["reddit", "twitter"])

    async def worker(kw, plat):
        events.append(f"start:{plat}")
        await asyncio.sleep(0.02)
        events.append(f"end:{plat}")
        return "ok"

    for kw in ("a", "b"):
        q.enqueue(kw, "twitter", worker=worker)
        q.enqueue(kw, "reddit", worker=worker)

    await q.run()

    # reddit 的所有 end 必须早于 twitter 的所有 start
    reddit_end_idx = max(i for i, e in enumerate(events) if e == "end:reddit")
    twitter_start_idx = min(i for i, e in enumerate(events) if e == "start:twitter")
    assert reddit_end_idx < twitter_start_idx


@pytest.mark.asyncio
async def test_snapshot_progress():
    q = KeywordJobQueue(concurrency=1)

    async def worker(kw, plat):
        await asyncio.sleep(0.01)
        return "ok"

    for kw in ("a", "b", "c"):
        q.enqueue(kw, "twitter", worker=worker)

    initial = q.snapshot()
    assert initial.total == 3
    assert initial.pending == 3

    await q.run()
    final = q.snapshot()
    assert final.completed == 3
    assert final.pending == 0
    assert final.as_dict()["percent"] == 100.0
