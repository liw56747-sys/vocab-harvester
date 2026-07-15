"""定时调度器"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.common.config import get_settings
from src.common.models import CrawlQuery, Platform
from src.orchestrator.pipeline import Pipeline

logger = logging.getLogger(__name__)


class Scheduler:
    """基于 APScheduler 的定时任务调度器"""

    def __init__(self, pipeline: Pipeline):
        self.pipeline = pipeline
        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        """启动定时调度"""
        settings = get_settings()

        if not settings.scheduler.enabled:
            logger.info("调度器未启用（配置 scheduler.enabled = false）")
            return

        # 解析 cron 表达式
        cron_parts = settings.scheduler.cron.split()
        if len(cron_parts) != 5:
            logger.error(f"无效的 cron 表达式: {settings.scheduler.cron}")
            return

        minute, hour, day, month, day_of_week = cron_parts

        self.scheduler.add_job(
            self._run_scheduled_job,
            "cron",
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            id="vocab_harvest_job",
            name="词库采集定时任务",
        )

        self.scheduler.start()
        logger.info(f"调度器已启动，cron: {settings.scheduler.cron}")

    def stop(self) -> None:
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("调度器已停止")

    async def _run_scheduled_job(self) -> None:
        """执行定时采集任务"""
        settings = get_settings()
        queries = settings.scheduler.default_queries or ["技术", "科技", "互联网"]

        # 从 pipeline 实际加载的爬虫动态获取可用平台，避免硬编码不存在的平台
        available_platforms = [
            Platform(p) for p in self.pipeline.crawlers.keys()
            if p in {plat.value for plat in Platform}
        ]
        if not available_platforms:
            logger.warning("[定时任务] 无可用的爬虫平台，跳过本次执行")
            return

        query = CrawlQuery(
            platforms=available_platforms,
            keywords=queries,
            max_results=50,
        )

        logger.info(f"[定时任务] 开始采集，平台: {[p.value for p in available_platforms]}，关键词: {queries}")
        stats = await self.pipeline.run(query, task_name="定时任务")
        
        # 处理黑词提取任务
        if hasattr(self.pipeline, 'vocab_manager'):
            self.pipeline.vocab_manager.process_blacklist(stats.get('results', []), "定时任务")
        
        logger.info(f"[定时任务] 完成: {stats['status']}, "
                     f"帖子: {stats['total_posts']}, "
                     f"关键词: {stats['total_keywords']}")
