"""
KeywordJobQueue — 关键词级抓取任务队列

用于大规模批量搜索（20–30 关键词 × 双平台 × 50+ 条），提供：
- 关键词粒度的独立任务（单个失败不牵连整批）
- Worker 并发（可配置），配合 BrowserPool 分散压力
- 指数退避重试（最多 2 次）
- 平台错峰：可选先 Reddit 后 Twitter，避免代理/IP 抢占
- 实时进度：completed / total / retrying / failed
- 全局取消：中途取消能立即停止未启动的 job，同时通知运行中的 job

设计原则：
- 与业务解耦：抓取函数以 async callable 注入
- 内存优先：状态不落盘，重启即丢；崩溃恢复可后续加 SQLite
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class KeywordJob:
    """一个关键词 × 一个平台 的抓取任务"""

    keyword: str
    platform: str
    index: int                       # 在批次中的顺序
    status: JobStatus = JobStatus.PENDING
    retries: int = 0
    max_retries: int = 2
    result: Any = None
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    # 供 worker 记录：当前指数退避的等待秒数
    _next_backoff: float = 2.0

    def as_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "platform": self.platform,
            "index": self.index,
            "status": self.status.value,
            "retries": self.retries,
            "error": self.error,
            "elapsed": (self.finished_at or time.time()) - self.started_at if self.started_at else 0,
        }


@dataclass
class QueueProgress:
    """整个批次的实时进度快照"""

    total: int = 0
    completed: int = 0
    failed: int = 0
    running: int = 0
    pending: int = 0
    cancelled: int = 0
    retries_total: int = 0
    current_keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "running": self.running,
            "pending": self.pending,
            "cancelled": self.cancelled,
            "retries_total": self.retries_total,
            "current_keywords": list(self.current_keywords),
            "percent": round(100 * self.completed / self.total, 1) if self.total else 0,
        }


class KeywordJobQueue:
    """
    关键词级任务队列。

    典型用法：
        q = KeywordJobQueue(concurrency=3, is_cancelled=lambda: False)
        q.enqueue("apple", "twitter", worker=my_fetch_fn)
        q.enqueue("banana", "reddit", worker=my_fetch_fn)
        results = await q.run()

    - worker: async callable，签名 `async def(keyword, platform) -> Any`
    - is_cancelled: 回调，返回 True 时立即停止未启动的 job
    """

    def __init__(
        self,
        concurrency: int = 3,
        is_cancelled: Callable[[], bool] | None = None,
        platform_order: list[str] | None = None,
        batch_size: int = 0,
        batch_cooldown: float = 30.0,
    ):
        """
        Args:
            concurrency: 同时运行的 worker 数
            is_cancelled: 全局取消检查器（每 job 启动前+失败重试前都检查）
            platform_order: 平台执行顺序，靠前的先跑完再启后面的（错峰）
            batch_size: 每平台组内再切分的批大小（>0 生效，用于大规模抓取分批）
            batch_cooldown: 批次间的冷却时间（秒），让浏览器/代理喘口气
        """
        self.concurrency = max(1, concurrency)
        self.is_cancelled = is_cancelled or (lambda: False)
        self.platform_order = platform_order or []
        self.batch_size = max(0, batch_size)
        self.batch_cooldown = max(0.0, batch_cooldown)
        self._jobs: list[KeywordJob] = []
        self._retries_total = 0

    # ── 提交任务 ──────────────────────────────────────────

    def enqueue(
        self,
        keyword: str,
        platform: str,
        worker: Callable[[str, str], Awaitable[Any]],
        max_retries: int = 2,
    ):
        """添加一个关键词-平台 job；worker 绑定到 job 上"""
        job = KeywordJob(
            keyword=keyword,
            platform=platform,
            index=len(self._jobs),
            max_retries=max_retries,
        )
        job.worker = worker  # type: ignore[attr-defined]
        self._jobs.append(job)
        return job

    def snapshot(self) -> QueueProgress:
        """获取当前进度快照（线程安全，仅读元数据）"""
        p = QueueProgress(total=len(self._jobs), retries_total=self._retries_total)
        for j in self._jobs:
            if j.status == JobStatus.PENDING:
                p.pending += 1
            elif j.status == JobStatus.RUNNING:
                p.running += 1
                p.current_keywords.append(f"{j.keyword}@{j.platform}")
            elif j.status == JobStatus.SUCCESS:
                p.completed += 1
            elif j.status == JobStatus.FAILED:
                p.failed += 1
            elif j.status == JobStatus.CANCELLED:
                p.cancelled += 1
        # 视 completed 为"已完成"总数（含失败/取消，用于进度百分比）
        p.completed = p.completed + p.failed + p.cancelled
        return p

    def results(self) -> list[KeywordJob]:
        """返回所有 job 副本"""
        return list(self._jobs)

    # ── 运行 ──────────────────────────────────────────────

    async def run(self, progress_callback: Callable[[QueueProgress], None] | None = None) -> list[KeywordJob]:
        """执行队列，返回所有 jobs 的最终状态"""
        if not self._jobs:
            return []

        # 按平台错峰分组
        groups: list[list[KeywordJob]] = []
        if self.platform_order:
            for plat in self.platform_order:
                grp = [j for j in self._jobs if j.platform == plat]
                if grp:
                    groups.append(grp)
            # 未在 order 中的平台放最后
            other = [j for j in self._jobs if j.platform not in self.platform_order]
            if other:
                groups.append(other)
        else:
            groups.append(list(self._jobs))

        for grp in groups:
            if self.is_cancelled():
                self._mark_pending_as_cancelled()
                break
            await self._run_group(grp, progress_callback)

        return self._jobs

    async def _run_group(
        self,
        group: list[KeywordJob],
        progress_callback: Callable[[QueueProgress], None] | None,
    ):
        """在一个平台组内执行 jobs；若 batch_size > 0 则再切分为子批次串行执行"""
        # 未启用分批 or 数量不需要分批 → 一次并发跑完
        if self.batch_size == 0 or len(group) <= self.batch_size:
            await self._run_sub_batch(group, progress_callback)
            return

        # 大规模：切分为多个 sub-batch 串行，批间冷却
        for i in range(0, len(group), self.batch_size):
            if self.is_cancelled():
                return
            sub = group[i:i + self.batch_size]
            logger.info(
                f"KeywordJobQueue: 执行子批次 {i // self.batch_size + 1}/"
                f"{(len(group) + self.batch_size - 1) // self.batch_size} "
                f"(size={len(sub)})"
            )
            await self._run_sub_batch(sub, progress_callback)

            # 除最后一批外，批间冷却（可被取消打断）
            if i + self.batch_size < len(group) and self.batch_cooldown > 0:
                cooled = 0.0
                logger.info(f"KeywordJobQueue: 批间冷却 {self.batch_cooldown}s")
                while cooled < self.batch_cooldown:
                    if self.is_cancelled():
                        return
                    await asyncio.sleep(0.5)
                    cooled += 0.5

    async def _run_sub_batch(
        self,
        sub: list[KeywordJob],
        progress_callback: Callable[[QueueProgress], None] | None,
    ):
        """并发执行一个子批次内的所有 job（受 concurrency 限流）"""
        sem = asyncio.Semaphore(self.concurrency)

        async def _one(job: KeywordJob):
            async with sem:
                if self.is_cancelled():
                    if job.status == JobStatus.PENDING:
                        job.status = JobStatus.CANCELLED
                    return
                await self._execute_with_retry(job)
                if progress_callback:
                    try:
                        progress_callback(self.snapshot())
                    except Exception:
                        pass

        await asyncio.gather(*[_one(j) for j in sub], return_exceptions=True)

    async def _execute_with_retry(self, job: KeywordJob):
        """执行单个 job，失败按指数退避重试"""
        worker: Callable[[str, str], Awaitable[Any]] = getattr(job, "worker", None)
        if worker is None:
            job.status = JobStatus.FAILED
            job.error = "worker 未绑定"
            return

        job.started_at = time.time()
        while True:
            if self.is_cancelled():
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
                return

            job.status = JobStatus.RUNNING
            try:
                result = await worker(job.keyword, job.platform)
                job.result = result
                job.status = JobStatus.SUCCESS
                job.finished_at = time.time()
                return
            except asyncio.CancelledError:
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
                raise
            except Exception as e:
                job.error = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"Job[{job.index}] {job.keyword}@{job.platform} 失败 "
                    f"(retry {job.retries}/{job.max_retries}): {job.error}"
                )

                if job.retries >= job.max_retries:
                    job.status = JobStatus.FAILED
                    job.finished_at = time.time()
                    return

                # 指数退避 + 抖动，避免同时重试雪崩
                backoff = job._next_backoff
                job._next_backoff = min(backoff * 2, 30.0)
                job.retries += 1
                self._retries_total += 1

                # 分段 sleep：每 0.5s 检查一次取消
                waited = 0.0
                while waited < backoff:
                    if self.is_cancelled():
                        job.status = JobStatus.CANCELLED
                        job.finished_at = time.time()
                        return
                    await asyncio.sleep(0.5)
                    waited += 0.5

    def _mark_pending_as_cancelled(self):
        for j in self._jobs:
            if j.status == JobStatus.PENDING:
                j.status = JobStatus.CANCELLED
