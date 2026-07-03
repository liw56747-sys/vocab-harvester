"""调度器：串联采集→工作流→词库的完整流水线"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.adapter.base import WorkflowAdapter
from src.adapter.mock import MockAdapter
from src.adapter.xingpan import XingpanAdapter
from src.common.config import get_settings
from src.common.database import get_db
from src.common.models import CrawlQuery, Platform
from src.crawlers.base import BaseCrawler
from src.crawlers.mock import MockCrawler
from src.vocabulary.manager import VocabManager

logger = logging.getLogger(__name__)


class Pipeline:
    """主流水线编排器"""

    def __init__(
        self,
        crawlers: dict[str, BaseCrawler] | None = None,
        adapter: WorkflowAdapter | None = None,
        vocab_manager: VocabManager | None = None,
    ):
        self.crawlers = crawlers or {}
        self.adapter = adapter or MockAdapter()
        self.vocab_manager = vocab_manager or VocabManager()

    @classmethod
    def from_config(cls) -> Pipeline:
        """根据配置创建流水线"""
        settings = get_settings()

        # 创建爬虫实例 —— Mock 模式下为所有平台都创建爬虫
        crawlers: dict[str, BaseCrawler] = {}
        for platform in Platform:
            crawlers[platform.value] = MockCrawler(platform)
            logger.info(f"已加载爬虫: {platform.value} (mock)")

        # 创建适配器
        adapter: WorkflowAdapter
        if settings.workflow.adapter == "mock":
            adapter = MockAdapter()
        elif settings.workflow.adapter == "xingpan":
            adapter = XingpanAdapter()
        else:
            logger.warning(f"适配器 '{settings.workflow.adapter}' 尚未实现，使用 Mock")
            adapter = MockAdapter()

        return cls(crawlers=crawlers, adapter=adapter)

    @classmethod
    def from_config_with_model(
        cls,
        base_url: str,
        api_key: str,
        model: str,
        backup_model: str = "",
        backup_base_url: str = "",
        backup_api_key: str = "",
    ) -> Pipeline:
        """根据前端传入的模型配置创建流水线（用于导入时的 LLM 分析）"""
        from src.adapter.xingpan import XingpanAdapter, XingpanConfig

        config = XingpanConfig(
            base_url=base_url,
            api_key=api_key,
            model=model or "glm-4-plus",
            backup_model=backup_model,
            backup_base_url=backup_base_url or base_url,
            backup_api_key=backup_api_key or api_key,
        )
        adapter = XingpanAdapter(config=config)
        return cls(adapter=adapter)

    async def run(
        self,
        query: CrawlQuery,
    ) -> dict[str, Any]:
        """
        执行一次完整的数据处理流程。

        流程：采集 → 投递工作流 → 接收结果 → 写入词库

        Args:
            query: 采集查询参数

        Returns:
            本次运行统计信息
        """
        stats: dict[str, Any] = {
            "started_at": datetime.now().isoformat(),
            "platforms": [],
            "total_posts": 0,
            "total_keywords": 0,
            "status": "running",
            "debug_info": [],
        }

        # 记录采集日志
        db = await get_db()
        cursor = await db.execute(
            """INSERT INTO crawl_log (platform, query_keywords, started_at, status)
               VALUES (?, ?, ?, 'running')""",
            (
                ",".join(p.value for p in query.platforms),
                str(query.keywords),
                datetime.now().isoformat(),
            ),
        )
        log_id = cursor.lastrowid
        await db.commit()

        try:
            # ── 阶段一：采集 ──
            all_posts = []
            stats["debug_info"].append(f"Processing {len(query.platforms)} platforms: {[p.value for p in query.platforms]}")
            stats["debug_info"].append(f"Available crawlers: {list(self.crawlers.keys())}")
            
            for platform in query.platforms:
                crawler = self.crawlers.get(platform.value)
                if not crawler:
                    logger.warning(f"未找到平台 {platform.value} 的爬虫，跳过")
                    stats["debug_info"].append(f"No crawler for {platform.value}, skipping")
                    continue

                stats["debug_info"].append(f"Fetching from {platform.value}...")
                logger.info(f"开始采集: {platform.value}, 关键词: {query.keywords}")
                posts = await crawler.fetch(query)
                stats["debug_info"].append(f"{platform.value} returned {len(posts)} posts")
                all_posts.extend(posts)
                stats["platforms"].append({
                    "name": platform.value,
                    "post_count": len(posts),
                })
                logger.info(f"  {platform.value} 采集到 {len(posts)} 条数据")

            stats["total_posts"] = len(all_posts)

            # 添加采集到的帖子摘要（最多50条）
            stats["sampled_posts"] = [
                {
                    "platform": p.platform,
                    "post_id": p.post_id,
                    "author": p.author,
                    "content": p.content[:200] if len(p.content) > 200 else p.content,
                    "published_at": p.published_at.isoformat() if p.published_at else "",
                    "metrics": p.metrics,
                    "tags": p.tags,
                }
                for p in all_posts[:50]
            ]

            if not all_posts:
                stats["status"] = "empty"
                logger.warning("未采集到任何数据")
                await self._update_log(db, log_id, stats)
                return stats

            # ── 阶段二：投递给工作流 ──
            logger.info(f"投递 {len(all_posts)} 条数据到工作流...")
            result = await self.adapter.submit_and_wait(all_posts)
            stats["total_keywords"] = len(result.keywords)
            stats["workflow_metadata"] = result.metadata
            logger.info(f"工作流提取出 {len(result.keywords)} 个关键词")

            # ── 阶段三：写入词库 ──
            logger.info("写入词库...")
            ingested = await self.vocab_manager.ingest(result, source_posts=all_posts)
            stats["ingested_count"] = ingested
            logger.info(f"词库更新 {ingested} 条记录")

            stats["status"] = "success"

        except Exception as e:
            stats["status"] = "failed"
            stats["error"] = str(e)
            logger.error(f"流水线执行失败: {e}", exc_info=True)

        stats["finished_at"] = datetime.now().isoformat()
        await self._update_log(db, log_id, stats)

        return stats

    async def _update_log(self, db, log_id: int, stats: dict) -> None:
        """更新采集日志"""
        await db.execute(
            """UPDATE crawl_log
               SET post_count = ?, finished_at = ?, status = ?, error_msg = ?
               WHERE id = ?""",
            (
                stats.get("total_posts", 0),
                datetime.now().isoformat(),
                stats.get("status", "unknown"),
                stats.get("error", ""),
                log_id,
            ),
        )
        await db.commit()

    async def process_posts(
        self,
        posts: list[ParsedPost],
        source: str = "import",
    ) -> dict[str, Any]:
        """
        直接处理已有的帖子数据（跳过采集阶段）。
        用于导入外部数据文件。
        """
        stats: dict[str, Any] = {
            "started_at": datetime.now().isoformat(),
            "total_posts": len(posts),
            "total_keywords": 0,
            "status": "running",
            "sampled_posts": [
                {
                    "platform": p.platform,
                    "post_id": p.post_id,
                    "author": p.author,
                    "content": p.content[:200] if len(p.content) > 200 else p.content,
                    "published_at": p.published_at.isoformat() if p.published_at else "",
                    "metrics": p.metrics,
                    "tags": p.tags,
                }
                for p in posts[:50]
            ],
        }

        db = await get_db()
        cursor = await db.execute(
            """INSERT INTO crawl_log (platform, query_keywords, started_at, status)
               VALUES (?, ?, ?, 'running')""",
            (source, "file_import", datetime.now().isoformat()),
        )
        log_id = cursor.lastrowid
        await db.commit()

        try:
            if not posts:
                stats["status"] = "empty"
                await self._update_log(db, log_id, stats)
                return stats

            logger.info(f"导入 {len(posts)} 条数据到工作流...")
            result = await self.adapter.submit_and_wait(posts)
            stats["total_keywords"] = len(result.keywords)
            stats["workflow_metadata"] = result.metadata

            logger.info("写入词库...")
            ingested = await self.vocab_manager.ingest(result, source_posts=posts)
            stats["ingested_count"] = ingested

            stats["status"] = "success"

        except Exception as e:
            stats["status"] = "failed"
            stats["error"] = str(e)
            logger.error(f"导入处理失败: {e}", exc_info=True)

        stats["finished_at"] = datetime.now().isoformat()
        await self._update_log(db, log_id, stats)

        return stats
