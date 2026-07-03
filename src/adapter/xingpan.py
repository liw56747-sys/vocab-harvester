"""
星盘工作流适配器 — 智谱 GLM 版本

直接调用智谱大模型 API 执行"黑词补充--舆情"工作流的核心逻辑：
  1. 本地文本清洗（复用星盘工作流的清洗脚本）
  2. 调用智谱 GLM 提取候选黑词
  3. 解析 LLM 返回的结构化 JSON
  4. 返回 WorkflowResult
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from src.adapter.base import WorkflowAdapter
from src.common.config import get_settings
from src.common.models import (
    ExtractedKeyword,
    ParsedPost,
    WorkflowResult,
)

logger = logging.getLogger(__name__)


# ── 配置 ──────────────────────────────────────────────────

@dataclass
class XingpanConfig:
    """智谱 API 配置"""
    base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    api_key: str = ""
    model: str = "glm-4-plus"
    backup_model: str = "glm-4-flash"
    backup_base_url: str = ""
    backup_api_key: str = ""
    temperature: float = 0.3
    max_tokens: int = 6000
    timeout: int = 300
    batch_size: int = 10  # 每批提交的文本数量

    @classmethod
    def from_settings(cls) -> XingpanConfig:
        settings = get_settings()
        wf = settings.workflow
        return cls(
            base_url=wf.api.base_url or "https://open.bigmodel.cn/api/paas/v4",
            api_key=wf.api.api_key,
            model=wf.api.model or wf.api.workflow_id or "glm-4-plus",
            backup_model=wf.api.backup_model or "glm-4-flash",
            timeout=wf.api.timeout,
            batch_size=wf.api.batch_size,
        )


# ── 文本清洗（复用星盘工作流中的 Python 脚本逻辑）────────

_MIN_TEXT_LEN = 1
_MAX_TEXT_LEN = 5000


def _to_half_width(text: str) -> str:
    result = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            result.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    return "".join(result)


def _clean_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text)
    text = _to_half_width(text)
    text = re.sub(r"[\u200b\u200c\u2060\ufeff]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"([!！?？。,.，、~～])\1{2,}", r"\1\1", text)
    text = text.strip()
    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN]
    return text


# ── 系统提示词（来自星盘工作流） ─────────────────────────

_SYSTEM_PROMPT = """你是一名内容安全词库分析助手。

你的任务是基于【UGC 文本】，发现可能需要进入专项词库的新增候选表达（黑词、变体词、风险短语、上下文依赖表达）。

## 一、处理说明

* 本任务不是判断整条文本是否违规。
* 本任务只提取"可能需要进入专项词库的新增候选词、疑似黑词、变体词、风险短语或上下文依赖表达"。

## 二、内部处理流程

你必须按以下顺序判断，但最终只输出 JSON：

1. 先逐字查看 UGC 文本中真实出现的表达，不得补全、扩写或联想输入中没有出现的词。
2. 判断是否存在舆情风险语境，包括事件传播、求瓜求资源、爆料搬运、截图扩散、人物机构指代、群体对立、煽动围观、网暴开盒、谣言化传播、反讽影射、规避审核表达等。
3. 若存在舆情风险语境，再提取最小可入库单元。
4. 提取 candidate 前，必须判断该词是否属于日常高频词、普通情绪词、普通网络流行语、普通名词或普通动词。
5. 如果 candidate 在日常生活场景中使用频率很高，且脱离事件语境后风险含义不稳定，不要作为独立黑词输出。
6. 若短词已经能稳定表达风险，且误伤风险较低，可以输出短词。
7. 若短词本身是高频日常词，脱离事件背景不具备风险，但组合短语结合上下文后具备风险，则输出组合短语，并将 type 设为 context_phrase，match_type 设为 context_only。
8. 若只是普通吐槽、普通情绪表达、普通新闻讨论、普通民生反馈、普通品牌提及，且没有煽动扩散、谣言化、对立化、攻击化、网暴化或事件聚集风险，不输出。
9. 若文本中存在事件别称、当事人代称、地点代称、机构代称、截图搬运语、求瓜求链接、爆料暗号、谐音缩写、emoji、数字、拼音、插符号等传播变体，应优先提取。
10. 若同一文本中存在多个候选词，但本质一致、只是长短不同、修饰词不同或包含关系不同，只保留最小核心候选词。
11. 若没有任何新增候选词，只输出空 results 数组。

## 三、重点识别类型

1. 事件核心词
   包括事件名称、别称、简称、缩写、谐音、错别字、拼音、外文、数字化、emoji 化、符号化表达。

2. 事件相关主体
   包括事件中的人物、组织、机构、地点、平台、品牌、作品、群体等相关主体的异常称呼、代称、黑话、隐喻、变体表达。

3. 风险观点与口号
   包括围绕事件形成的攻击性表达、煽动性表达、对立性表达、口号化表达、立场动员表达、情绪化标签。

4. 隐晦影射表达
   包括不直接提及事件，但通过时间、地点、人物特征、符号、梗、暗号、截图搬运语、反讽语气等方式指向该事件的表达。

5. 传播扩散表达
   包括求资源、求原图、求视频、求链接、求瓜、搬运、存档、合集、时间线、爆料、内幕、避雷、挂人、开盒等事件传播相关表达。

6. 规避审核变体
   包括插符号、拆字、谐音、拼音、首字母、错别字、数字代称、emoji 替代、外文夹杂、繁简变换、同音替换等规避表达。

7. 上下文依赖短语
   单独看不一定有风险，但结合事件背景或上下文后，明显指向该热点事件或相关风险。

## 四、严格规则

1. 只能从输入内容中提取候选词或短语，禁止凭空编造。
2. 不要补全、扩写或联想输入中没有出现的敏感词。
3. 普通人名、地名、机构名、品牌名、事件名，如果没有风险语境、隐晦指代、异常表达，不要输出。
4. 对昵称、简介、话题标签、评论、标题中的异常表达要重点关注。
5. candidate 必须是"最小可入库单元"，不要把整句话、长修饰语、情绪化修饰词作为 candidate。
6. 如果多个候选词本质一致，只是长短不同、修饰词不同、包含关系不同，只保留一个核心候选词。
7. 如果候选词 A 被候选词 B 包含，且二者风险指向一致，只输出更适合作为词库项的核心词，不要同时输出 A 和 B。
8. 长表达中的修饰语、反讽语气、上下文描述，应放入 evidence 或 reason，不要单独作为 candidate。
9. 同一条文本中，同一核心表达只能输出一次。
10. 如果候选词必须结合事件背景或上下文才有风险，type 必须为 context_phrase，match_type 必须为 context_only。
11. 如果只是泛泛提及、风险不明确，但可能有观察价值，action 设为 observe。
12. 输出必须是合法 JSON，不要输出 Markdown，不要输出解释性文字，不要在 JSON 外输出任何内容。

## 五、候选词保留规则

1. 只输出文本中符合专项词库要求的新增候选词。
2. 如果短词已经能表达风险含义，不要再输出包含该短词的长短语。
3. 只有当长短语的整体组合含义发生变化，且短词无法表达该风险时，才允许输出长短语。
4. 与热点事件强相关、传播中频繁出现、存在规避审核特征的候选词，应优先输出。
5. 与热点事件无关的泛风险词、普通情绪词、普通讨论词，不要输出。
6. 如果表达只是普通围观、普通吃瓜、普通评论，但无法指向风险，不要输出。

## 六、输出字段说明

* source_id：原始文本 ID
* source_type：文本来源类型，如 post/comment/nickname/bio/topic/title/unknown
* candidate：新增候选词或候选短语，必须是最小可入库单元
* type：候选类型，只能是 event_word/entity_variant/slogan/risk_phrase/context_phrase/evasion_variant
* category：候选类别，只能是 事件核心词/相关主体/风险观点口号/隐晦影射/传播扩散/规避变体/上下文依赖/其他/unknown
* evidence：原文证据片段
* reason：简短原因，30 字以内
* score：风险分，0-100
* action：建议动作，只能是 add_temp_kb/observe/need_human_review/reject
* match_type：建议匹配方式，只能是 exact/contains/regex/context_only/not_recommended

所有结果放在 "results" 数组中：
{
  "results": [
    {
      "source_id": "",
      "source_type": "",
      "candidate": "",
      "type": "",
      "category": "",
      "evidence": "",
      "reason": "",
      "score": 0,
      "action": "need_human_review",
      "match_type": "context_only"
    }
  ]
}

如果没有发现任何候选词，输出：
{"results": []}"""


# ── 适配器实现 ────────────────────────────────────────────

class XingpanAdapter(WorkflowAdapter):
    """
    通过智谱 GLM API 执行黑词提取。

    复用星盘工作流中的提示词和文本清洗逻辑，
    直接调用智谱大模型完成候选词识别。
    """

    def __init__(self, config: XingpanConfig | None = None):
        self.config = config or XingpanConfig.from_settings()
        self._client: AsyncOpenAI | None = None
        self._backup_client: AsyncOpenAI | None = None
        self._pending_results: dict[str, WorkflowResult] = {}

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
            )
        return self._client

    def _get_backup_client(self) -> AsyncOpenAI:
        if self._backup_client is None:
            backup_url = self.config.backup_base_url or self.config.base_url
            backup_key = self.config.backup_api_key or self.config.api_key
            self._backup_client = AsyncOpenAI(
                base_url=backup_url,
                api_key=backup_key,
                timeout=self.config.timeout,
            )
        return self._backup_client

    async def submit(self, posts: list[ParsedPost]) -> str:
        """
        将帖子数据提交给智谱 GLM 进行黑词提取。

        流程：
        1. 清洗文本
        2. 按 batch_size 分批
        3. 构造 prompt 调用 LLM
        4. 解析结果
        """
        task_id = str(uuid.uuid4())[:12]
        all_keywords: list[ExtractedKeyword] = []
        post_ids = [p.post_id for p in posts]

        # 清洗并构建文本列表
        cleaned_items = []
        for post in posts:
            clean_text = _clean_text(post.content)
            if len(clean_text) >= _MIN_TEXT_LEN:
                cleaned_items.append({
                    "id": post.post_id,
                    "source_type": post.platform,
                    "text": clean_text,
                })

        if not cleaned_items:
            logger.warning("清洗后无有效文本")
            self._pending_results[task_id] = WorkflowResult(
                task_id=task_id, keywords=[], source_posts=post_ids,
            )
            return task_id

        logger.info(f"清洗后有效文本 {len(cleaned_items)} 条，开始分批调用 LLM")

        # 分批调用
        for batch_start in range(0, len(cleaned_items), self.config.batch_size):
            batch = cleaned_items[batch_start:batch_start + self.config.batch_size]
            batch_keywords = await self._call_llm(batch)
            all_keywords.extend(batch_keywords)
            logger.info(f"  批次 {batch_start // self.config.batch_size + 1}: "
                        f"提取 {len(batch_keywords)} 个候选词")

        self._pending_results[task_id] = WorkflowResult(
            task_id=task_id,
            keywords=all_keywords,
            source_posts=post_ids,
            metadata={
                "total_posts": len(posts),
                "cleaned_posts": len(cleaned_items),
                "model": self.config.model,
            },
        )

        return task_id

    async def _call_llm(self, items: list[dict]) -> list[ExtractedKeyword]:
        """调用智谱 GLM 提取一批文本中的候选词"""
        # 构造用户消息：将多条文本拼接
        texts_block = "\n\n".join(
            f"[ID:{item['id']}] [{item['source_type']}] {item['text']}"
            for item in items
        )
        user_message = f"待分析 UGC 文本如下：\n\n{texts_block}"

        client = self._get_client()

        try:
            response = await client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                stream=False,
            )

            content = response.choices[0].message.content or ""
            return self._parse_llm_response(content)

        except Exception as e:
            logger.error(f"智谱 API 调用失败: {e}")
            # 尝试备用模型
            if self.config.backup_model and self.config.backup_model != self.config.model:
                logger.info(f"尝试备用模型: {self.config.backup_model}")
                try:
                    backup_client = self._get_backup_client()
                    response = await backup_client.chat.completions.create(
                        model=self.config.backup_model,
                        messages=[
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": user_message},
                        ],
                        temperature=self.config.temperature,
                        max_tokens=self.config.max_tokens,
                        stream=False,
                    )
                    content = response.choices[0].message.content or ""
                    return self._parse_llm_response(content)
                except Exception as e2:
                    logger.error(f"备用模型也失败: {e2}")
            return []

    @staticmethod
    def _parse_llm_response(content: str) -> list[ExtractedKeyword]:
        """解析 LLM 返回的 JSON 内容"""
        # 尝试提取 JSON（LLM 可能会在 JSON 外包裹 markdown 代码块）
        json_str = content.strip()

        # 去除 markdown 代码块包裹
        if json_str.startswith("```"):
            json_str = re.sub(r"^```(?:json)?\s*", "", json_str)
            json_str = re.sub(r"\s*```$", "", json_str)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON 对象
            match = re.search(r'\{[^{}]*"results"\s*:\s*\[.*?\]\s*\}', json_str, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    logger.warning(f"无法解析 LLM 返回的 JSON: {content[:200]}...")
                    return []
            else:
                logger.warning(f"LLM 返回内容不包含有效 JSON: {content[:200]}...")
                return []

        # 从 results 数组提取候选词
        results = data.get("results", [])
        if not isinstance(results, list):
            return []

        keywords: list[ExtractedKeyword] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            kw = _parse_candidate_item(item)
            if kw:
                keywords.append(kw)

        return keywords

    async def poll_result(self, task_id: str) -> WorkflowResult | None:
        """获取处理结果（同步调用模式下直接返回缓存结果）"""
        return self._pending_results.pop(task_id, None)

    async def receive_callback(self, payload: dict) -> WorkflowResult:
        """接收回调结果（智谱 API 为同步模式，此方法暂不常用）"""
        task_id = payload.get("task_id", str(uuid.uuid4()))
        content = payload.get("content", payload.get("output", ""))
        keywords = self._parse_llm_response(str(content))
        return WorkflowResult(task_id=task_id, keywords=keywords)

    async def close(self) -> None:
        """关闭客户端"""
        if self._client:
            await self._client.close()
            self._client = None


def _parse_candidate_item(item: dict) -> ExtractedKeyword | None:
    """解析单个候选词 JSON 对象"""
    candidate = str(item.get("candidate", "")).strip()
    if not candidate or candidate == "空":
        return None

    score = item.get("score", 0)
    if isinstance(score, str):
        try:
            score = int(float(score))
        except (ValueError, TypeError):
            score = 0

    return ExtractedKeyword(
        candidate=candidate,
        source_id=str(item.get("source_id", "")),
        source_type=str(item.get("source_type", "")),
        type=str(item.get("type", "unknown")),
        category=str(item.get("category", "unknown")),
        evidence=str(item.get("evidence", "")),
        reason=str(item.get("reason", "")),
        score=int(score),
        action=str(item.get("action", "need_human_review")),
        match_type=str(item.get("match_type", "context_only")),
    )
