"""公共数据模型定义"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Platform(str, Enum):
    WEIBO = "weibo"
    XIAOHONGSHU = "xiaohongshu"
    TWITTER = "twitter"


@dataclass
class CrawlQuery:
    """采集请求参数"""
    platforms: list[Platform]
    keywords: list[str]
    max_results: int = 100
    since: datetime | None = None
    until: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ParsedPost:
    """采集到的帖子统一格式"""
    platform: str
    post_id: str
    content: str
    author: str
    published_at: datetime
    metrics: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "post_id": self.post_id,
            "content": self.content,
            "author": self.author,
            "published_at": self.published_at.isoformat(),
            "metrics": self.metrics,
            "tags": self.tags,
        }


@dataclass
class ExtractedKeyword:
    """
    工作流提取出的单个候选词。

    字段与星盘工作流 "黑词补充--舆情" 的输出格式对齐。
    """
    candidate: str                              # 候选词/短语（最小可入库单元）
    source_id: str = ""                          # 原始文本 ID
    source_type: str = ""                        # 来源类型: post/comment/nickname/bio/topic/title/unknown
    type: str = "unknown"                        # 候选类型: event_word/entity_variant/slogan/risk_phrase/context_phrase/evasion_variant
    category: str = "unknown"                    # 类别: 事件核心词/相关主体/风险观点口号/隐晦影射/传播扩散/规避变体/上下文依赖/其他/unknown
    evidence: str = ""                           # 原文证据片段
    reason: str = ""                             # 简短原因
    score: int = 0                               # 风险分 (0-100)
    action: str = "need_human_review"            # 建议动作: add_temp_kb/observe/need_human_review/reject
    match_type: str = "context_only"             # 匹配方式: exact/contains/regex/context_only/not_recommended

    @property
    def word(self) -> str:
        """兼容旧接口"""
        return self.candidate

    @property
    def confidence(self) -> float:
        """兼容旧接口，将 0-100 分数映射到 0-1"""
        return self.score / 100.0

    @classmethod
    def from_workflow(cls, data: dict) -> ExtractedKeyword:
        """从星盘工作流 JSON 结果构建"""
        return cls(
            candidate=str(data.get("candidate", "")).strip(),
            source_id=str(data.get("source_id", "")),
            source_type=str(data.get("source_type", "")),
            type=str(data.get("type", "unknown")),
            category=str(data.get("category", "unknown")),
            evidence=str(data.get("evidence", "")),
            reason=str(data.get("reason", "")),
            score=int(data.get("score", 0)),
            action=str(data.get("action", "need_human_review")),
            match_type=str(data.get("match_type", "context_only")),
        )


@dataclass
class WorkflowResult:
    """工作流处理结果"""
    task_id: str
    keywords: list[ExtractedKeyword] = field(default_factory=list)
    source_posts: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowResult:
        """从完整 JSON 结果构建（星盘工作流的 results 数组）"""
        keywords = []
        for item in data.get("results", []):
            candidate = str(item.get("candidate", "")).strip()
            # 跳过兜底结果 "空"
            if not candidate or candidate == "空":
                continue
            keywords.append(ExtractedKeyword.from_workflow(item))

        return cls(
            task_id=data.get("task_id", str(uuid.uuid4())),
            keywords=keywords,
            source_posts=data.get("source_posts", []),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_candidate_score_string(cls, text: str, task_id: str = "") -> WorkflowResult:
        """
        从星盘工作流最终输出格式解析。

        工作流输出为 "candidate1：score1；candidate2：score2" 的字符串。
        这是简化格式，只包含候选词和分数。
        """
        import uuid as _uuid
        keywords = []

        if not text or text.strip() == "空":
            return cls(task_id=task_id or str(_uuid.uuid4()))

        pairs = text.split("；")
        for pair in pairs:
            pair = pair.strip()
            if not pair:
                continue
            parts = pair.rsplit("：", 1)
            if len(parts) == 2:
                candidate, score_str = parts[0].strip(), parts[1].strip()
                try:
                    score = int(float(score_str))
                except (ValueError, TypeError):
                    score = 0
                keywords.append(ExtractedKeyword(
                    candidate=candidate,
                    score=score,
                ))
            elif len(parts) == 1 and parts[0].strip():
                keywords.append(ExtractedKeyword(candidate=parts[0].strip()))

        return cls(
            task_id=task_id or str(_uuid.uuid4()),
            keywords=keywords,
        )


class VocabStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class VocabEntry:
    """词库词条"""
    word: str
    category: str = ""
    frequency: int = 1
    score: float = 0.0
    platforms: list[str] = field(default_factory=list)
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    context_samples: list[str] = field(default_factory=list)
    status: VocabStatus = VocabStatus.PENDING
    # 星盘工作流附加字段
    candidate_type: str = ""         # event_word/entity_variant/slogan/...
    source_type: str = ""            # post/comment/nickname/bio/...
    evidence: str = ""               # 原文证据
    reason: str = ""                 # 提取原因
    action: str = ""                 # add_temp_kb/observe/need_human_review/reject
    match_type: str = ""             # exact/contains/regex/context_only/...
