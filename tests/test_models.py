"""WorkflowResult 和 ExtractedKeyword 数据模型测试"""

import pytest

from src.common.models import WorkflowResult, ExtractedKeyword


async def test_candidate_score_string_basic():
    """解析 'candidate：score' 格式字符串"""
    text = "避坑军校：85；军校发配：75；劝退军校：60"
    result = WorkflowResult.from_candidate_score_string(text, "test_task_1")

    assert result.task_id == "test_task_1"
    assert len(result.keywords) == 3

    assert result.keywords[0].candidate == "避坑军校"
    assert result.keywords[0].score == 85
    assert result.keywords[1].candidate == "军校发配"
    assert result.keywords[1].score == 75
    assert result.keywords[2].candidate == "劝退军校"
    assert result.keywords[2].score == 60


async def test_candidate_score_string_empty():
    """空字符串返回空关键词列表"""
    result = WorkflowResult.from_candidate_score_string("", "test_task_2")
    assert len(result.keywords) == 0


async def test_candidate_score_string_placeholder():
    """'空' 占位符返回空关键词列表"""
    result = WorkflowResult.from_candidate_score_string("空", "test_task_3")
    assert len(result.keywords) == 0


async def test_candidate_score_string_single():
    """单个候选词解析"""
    result = WorkflowResult.from_candidate_score_string("军校劝退：70", "test_task_4")
    assert len(result.keywords) == 1
    assert result.keywords[0].candidate == "军校劝退"
    assert result.keywords[0].score == 70


async def test_candidate_score_string_no_score():
    """只有候选词没有分数时，分数默认为 0"""
    result = WorkflowResult.from_candidate_score_string("某个词", "test_task_5")
    assert len(result.keywords) == 1
    assert result.keywords[0].candidate == "某个词"
    assert result.keywords[0].score == 0


async def test_from_dict_full():
    """从完整 JSON dict 构建 WorkflowResult"""
    data = {
        "task_id": "task_100",
        "results": [
            {
                "candidate": "避坑军校",
                "type": "event_word",
                "category": "事件核心词",
                "score": 85,
                "action": "need_human_review",
                "match_type": "exact",
            },
            {"candidate": "空", "reason": "未发现"},
        ],
    }
    result = WorkflowResult.from_dict(data)

    assert result.task_id == "task_100"
    assert len(result.keywords) == 1  # "空" 被过滤
    assert result.keywords[0].candidate == "避坑军校"


async def test_extracted_keyword_properties():
    """ExtractedKeyword 兼容属性 word 和 confidence"""
    kw = ExtractedKeyword(candidate="测试词", score=80)
    assert kw.word == "测试词"
    assert kw.confidence == pytest.approx(0.8)


async def test_extracted_keyword_from_workflow():
    """从工作流 JSON dict 构建 ExtractedKeyword"""
    data = {
        "candidate": "军校发配",
        "source_id": "wb_001",
        "source_type": "post",
        "type": "context_phrase",
        "category": "隐晦影射",
        "evidence": "毕业后发配到偏远地区",
        "reason": "将正常分配污名化",
        "score": 75,
        "action": "add_temp_kb",
        "match_type": "context_only",
    }
    kw = ExtractedKeyword.from_workflow(data)
    assert kw.candidate == "军校发配"
    assert kw.type == "context_phrase"
    assert kw.score == 75
    assert kw.action == "add_temp_kb"
