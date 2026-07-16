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

    async def ingest(self, result: WorkflowResult, source_posts: list[ParsedPost] | None = None, task_name: str = "") -> int:
        """
        将工作流提取结果写入词库。

        Args:
            result: 工作流处理结果
            source_posts: 原始帖子列表（用于提取上下文例句）
            task_name: 关联的定时任务名称

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
                task_name=task_name,
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
        action: str | None = None,
        candidate_type: str | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        task_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """查询词库"""
        return await self.storage.query(
            keyword=keyword,
            category=category,
            status=status,
            platform=platform,
            action=action,
            candidate_type=candidate_type,
            score_min=score_min,
            score_max=score_max,
            task_name=task_name,
            limit=limit,
            offset=offset,
        )

    async def approve(self, word: str, category: str = "") -> bool:
        """审核通过词条"""
        return await self.storage.update_status(word, VocabStatus.APPROVED, category)

    async def reject(self, word: str, category: str = "") -> bool:
        """拒绝词条"""
        return await self.storage.update_status(word, VocabStatus.REJECTED, category)

    async def delete(self, word: str, category: str = "") -> bool:
        """删除词条"""
        return await self.storage.delete(word, category)

    async def get_stats(self) -> dict:
        """获取词库统计"""
        return await self.storage.get_stats()

    async def get_filter_options(self) -> dict:
        """获取筛选器选项"""
        return await self.storage.get_filter_options()

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

    def is_post_exist(self, post_id: str, keywords: str, user_id: str) -> bool:
        """检查特定维度的帖子是否已存在"""
        return self.storage.check_post_exists(
            post_id=post_id,
            keywords=keywords,
            user_id=user_id,
            date_range="30d"
        )

    def _is_similar(self, post1: ParsedPost, post2: ParsedPost) -> bool:
        """检查内容相似度（简化实现）"""
        # 实际实现应使用更复杂的文本相似度算法
        # 例如：Jaccard相似度、TF-IDF等
        return self._jaccard_similarity(post1.content, post2.content) > 0.7

    def _jaccard_similarity(self, text1: str, text2: str) -> float:
        """计算Jaccard相似度"""
        set1 = set(text1.split())
        set2 = set(text2.split())
        intersection = set1 & set2
        union = set1 | set2
        return len(intersection) / len(union) if union else 0.0

    async def ingest(self, result: WorkflowResult, source_posts: list[ParsedPost] | None = None, task_name: str = "") -> int:
        """将工作流提取结果写入词库。"""
        # ... (现有代码)

        # 1. 优先使用post_id去重
        if source_posts:
            seen_ids = set()
            new_posts = []
            for post in source_posts:
                if post.post_id:
                    if post.post_id in seen_ids:
                        continue  # 已存在，跳过
                    seen_ids.add(post.post_id)
                    new_posts.append(post)
                else:
                    new_posts.append(post)  # 无post_id，后续用内容去重

            source_posts = new_posts

        # 2. 内容相似度去重（针对无post_id的情况）
        if source_posts:
            unique_posts = self._deduplicate_by_content(source_posts)
            source_posts = unique_posts

        # 返回去重后需要写入的新帖子数量
        return len(source_posts)

    def _deduplicate_by_content(self, posts: list[ParsedPost]) -> list[ParsedPost]:
        """通过内容相似度去重（Jaccard相似度）"""
        unique_posts = []
        for post in posts:
            # 跳过已存在的相似内容
            if any(self._is_similar(post, p) for p in unique_posts):
                continue
            unique_posts.append(post)
        return unique_posts

    def process_blacklist(self, results: list, task_name: str):
        """处理黑词提取任务"""
        blacklist_items = self._extract_blacklist_items(results)
        if not blacklist_items:
            return

        self._save_blacklist(blacklist_items, task_name)
        self._notify_blacklist(task_name, len(blacklist_items))

    def _extract_blacklist_items(self, results: list) -> list:
        """核心黑词提取逻辑"""
        patterns = ["密码", "账号", "泄露"]  # 可从配置加载
        return [
            item['text']
            for item in results
            if any(p in item['text'] for p in patterns)
        ]

    def _save_blacklist(self, items, task_name):
        """存储到数据库（具体实现）"""
        # 执行 INSERT 操作
        pass

    def _notify_blacklist(self, task_name, count):
        """通知黑词提取结果"""
        logger.info(f"[黑词提取] 任务 '{task_name}' 检测到 {count} 条黑词")
