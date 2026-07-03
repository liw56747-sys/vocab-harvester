"""Mock 工作流适配器：模拟关键词提取，用于开发和测试"""

from __future__ import annotations

import random
import re
import uuid
from collections import Counter

from src.adapter.base import WorkflowAdapter
from src.common.models import ExtractedKeyword, ParsedPost, WorkflowResult


# 中文停用词
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "什么", "怎么", "为什么", "可以", "因为", "所以", "但是", "而且", "或者",
    "真的", "非常", "特别", "比较", "已经", "还", "太", "最", "更",
    "这个", "那个", "一个", "一下", "一些", "一下", "出来", "起来",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "this", "that", "these", "those", "i", "you", "he", "she", "it", "we",
    "they", "me", "him", "her", "us", "them", "my", "your", "his", "its",
    "our", "their", "what", "which", "who", "whom", "whose", "when", "where",
    "why", "how", "all", "each", "every", "here", "there", "about", "just",
}


class MockAdapter(WorkflowAdapter):
    """
    模拟星盘工作流适配器。

    使用简单的文本统计方法提取关键词，并模拟星盘工作流的输出格式。
    在实际部署中，替换为 XingpanAdapter。
    """

    def __init__(self, min_word_length: int = 2, top_k: int = 50):
        self.min_word_length = min_word_length
        self.top_k = top_k
        self._results: dict[str, WorkflowResult] = {}

    async def submit(self, posts: list[ParsedPost]) -> str:
        """模拟提交任务：立即处理并缓存结果"""
        task_id = str(uuid.uuid4())[:12]

        # 简单分词 + 词频统计
        word_counter: Counter = Counter()
        word_to_post: dict[str, ParsedPost] = {}  # 记录词首次出现的帖子
        post_ids: list[str] = []

        for post in posts:
            post_ids.append(post.post_id)
            words = self._tokenize(post.content)
            for w in words:
                if w not in word_to_post:
                    word_to_post[w] = post
            word_counter.update(words)

        # 模拟星盘工作流的输出格式
        keywords = []
        for word, count in word_counter.most_common(self.top_k):
            score = min(int(count * 10), 100)  # 模拟 0-100 风险分
            post = word_to_post.get(word)

            # 模拟工作流的分类逻辑
            candidate_type = random.choice([
                "event_word", "risk_phrase", "context_phrase", "entity_variant",
            ])
            category = random.choice([
                "事件核心词", "风险观点口号", "隐晦影射", "上下文依赖", "相关主体",
            ])
            action = "need_human_review" if score >= 50 else "observe"
            if score < 20:
                action = "reject"

            # 构造 evidence（原文片段）
            evidence = ""
            if post and word in post.content:
                idx = post.content.find(word)
                start = max(0, idx - 20)
                end = min(len(post.content), idx + len(word) + 20)
                evidence = post.content[start:end]

            keywords.append(ExtractedKeyword(
                candidate=word,
                source_id=post.post_id if post else "",
                source_type=post.platform if post else "unknown",
                type=candidate_type,
                category=category,
                evidence=evidence,
                reason=f"出现{count}次，疑似风险表达",
                score=score,
                action=action,
                match_type="context_only" if candidate_type == "context_phrase" else "contains",
            ))

        self._results[task_id] = WorkflowResult(
            task_id=task_id,
            keywords=keywords,
            source_posts=post_ids,
            metadata={"total_posts": len(posts), "unique_words": len(word_counter)},
        )

        return task_id

    async def poll_result(self, task_id: str) -> WorkflowResult | None:
        """模拟轮询：直接返回缓存的结果"""
        return self._results.get(task_id)

    def _tokenize(self, text: str) -> list[str]:
        """
        简易分词：
        - 中文：按连续中文字符序列拆分（bigram 模拟）
        - 英文：按空格拆分并转小写
        """
        words: list[str] = []

        # 提取英文单词
        english_words = re.findall(r'[a-zA-Z]+', text)
        words.extend(w.lower() for w in english_words if len(w.lower()) >= self.min_word_length)

        # 提取中文词组（简易 bigram）
        chinese_chars = re.findall(r'[\u4e00-\u9fff]+', text)
        for segment in chinese_chars:
            for i in range(len(segment) - 1):
                bigram = segment[i:i + 2]
                if bigram not in _STOP_WORDS:
                    words.append(bigram)
            # 也加入长度 >= 3 的连续片段作为候选
            for length in range(3, min(6, len(segment) + 1)):
                for i in range(len(segment) - length + 1):
                    ngram = segment[i:i + length]
                    words.append(ngram)

        # 过滤停用词
        words = [w for w in words if w not in _STOP_WORDS and len(w) >= self.min_word_length]

        return words
