"""词库管理器：业务逻辑层"""

from __future__ import annotations

import csv
import io
import json

from src.common.config import get_settings
from src.common.models import (
    ExtractedKeyword,
    ParsedPost,
    VocabEntry,
    VocabStatus,
    WorkflowResult,
)
from src.vocabulary.storage import VocabStorage


class VocabManager:
    """词库管理器：处理入库、审核、查询、导出等业务逻辑"""

    def __init__(self):
        self.storage = VocabStorage()
        self._config = get_settings().vocabulary

    async def ingest(self, result: WorkflowResult, source_posts: list[ParsedPost] | None = None) -> int:
        """
        将工作流提取结果写入词库。

        Args:
            result: 工作流处理结果
            source_posts: 原始帖子列表（用于提取上下文例句）

        Returns:
            新增/更新的词条数
        """
        # 构建 post_id -> content 映射（用于获取上下文）
        post_map: dict[str, str] = {}
        if source_posts:
            post_map = {p.post_id: p.content for p in source_posts}

        platforms: list[str] = []
        if source_posts:
            platforms = list(set(p.platform for p in source_posts))

        count = 0
        for kw in result.keywords:
            # 收集该词对应的上下文例句（优先使用工作流返回的 evidence，否则从原文搜索）
            samples = []
            if kw.evidence:
                samples = [kw.evidence]
            if source_posts:
                samples.extend(
                    self._collect_samples(kw.candidate, source_posts, max_samples=self._config.max_context_samples)
                )
            # 去重并截断
            seen: set[str] = set()
            unique_samples: list[str] = []
            for s in samples:
                if s not in seen:
                    seen.add(s)
                    unique_samples.append(s)
            samples = unique_samples[: self._config.max_context_samples]

            # 根据 action 映射审核状态
            status = VocabStatus.PENDING
            if kw.action == "reject":
                status = VocabStatus.REJECTED

            entry = VocabEntry(
                word=kw.candidate,
                category=kw.category or "",
                frequency=1,
                score=kw.score / 100.0,  # 工作流返回 0-100，存储为 0-1
                platforms=platforms,
                context_samples=samples,
                status=status,
                candidate_type=kw.type,
                source_type=kw.source_type,
                evidence=kw.evidence,
                reason=kw.reason,
                action=kw.action,
                match_type=kw.match_type,
            )

            await self.storage.upsert(entry)
            count += 1

        return count

    async def query(
        self,
        keyword: str | None = None,
        category: str | None = None,
        status: VocabStatus | None = None,
        platform: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """查询词库"""
        return await self.storage.query(
            keyword=keyword,
            category=category,
            status=status,
            platform=platform,
            limit=limit,
            offset=offset,
        )

    async def approve(self, word: str, category: str = "") -> bool:
        """审核通过词条"""
        return await self.storage.update_status(word, VocabStatus.APPROVED, category)

    async def reject(self, word: str, category: str = "") -> bool:
        """拒绝词条"""
        return await self.storage.update_status(word, VocabStatus.REJECTED, category)

    async def get_stats(self) -> dict:
        """获取词库统计"""
        return await self.storage.get_stats()

    async def export_json(self, status: VocabStatus | None = None) -> str:
        """导出为 JSON"""
        entries = await self.storage.export_all(status)
        return json.dumps(entries, ensure_ascii=False, indent=2)

    async def export_csv(self, status: VocabStatus | None = None) -> str:
        """导出为 CSV"""
        entries = await self.storage.export_all(status)
        if not entries:
            return ""

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=entries[0].keys())
        writer.writeheader()
        for entry in entries:
            # 将 list 字段序列化为字符串
            row = dict(entry)
            for key in ("platforms", "context_samples"):
                row[key] = json.dumps(row[key], ensure_ascii=False)
            writer.writerow(row)

        return output.getvalue()

    async def export_txt(self, status: VocabStatus | None = None) -> str:
        """导出为纯文本词库（每行一个词）"""
        entries = await self.storage.export_all(status)
        return "\n".join(e["word"] for e in entries)

    def _collect_samples(self, word: str, posts: list[ParsedPost], max_samples: int = 5) -> list[str]:
        """从帖子中收集包含该词的上下文例句"""
        samples: list[str] = []
        for post in posts:
            if word in post.content:
                # 截取包含该词的片段（前后各50字）
                idx = post.content.find(word)
                start = max(0, idx - 50)
                end = min(len(post.content), idx + len(word) + 50)
                snippet = post.content[start:end].strip()
                if snippet and snippet not in samples:
                    samples.append(snippet)
                if len(samples) >= max_samples:
                    break
        return samples
