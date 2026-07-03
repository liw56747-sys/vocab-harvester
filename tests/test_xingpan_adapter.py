"""XingpanAdapter 对 LLM 不同响应格式的解析测试"""

import json
import pytest

from src.adapter.xingpan import XingpanAdapter


async def test_parse_full_json():
    """解析完整 JSON 格式的 results 数组"""
    adapter = XingpanAdapter()
    response = json.dumps({
        "results": [
            {
                "source_id": "wb_12345",
                "source_type": "post",
                "candidate": "避坑军校",
                "type": "event_word",
                "category": "事件核心词",
                "evidence": "最近有人炒作避坑军校的话题",
                "reason": "疑似恶意炒作军校报考话题",
                "score": 85,
                "action": "need_human_review",
                "match_type": "exact",
            },
            {
                "source_id": "wb_12345",
                "source_type": "post",
                "candidate": "军校发配",
                "type": "context_phrase",
                "category": "隐晦影射",
                "evidence": "毕业后就是发配到偏远地区",
                "reason": "将正常分配污名化",
                "score": 75,
                "action": "add_temp_kb",
                "match_type": "context_only",
            },
            {
                "source_id": "",
                "candidate": "空",
                "reason": "未发现候选词",
            },
        ]
    })
    keywords = adapter._parse_llm_response(response)

    assert len(keywords) == 2  # "空" 被过滤

    kw1 = keywords[0]
    assert kw1.candidate == "避坑军校"
    assert kw1.type == "event_word"
    assert kw1.category == "事件核心词"
    assert kw1.score == 85
    assert kw1.action == "need_human_review"
    assert kw1.match_type == "exact"


async def test_parse_markdown_wrapped():
    """解析 markdown 代码块包裹的 JSON"""
    adapter = XingpanAdapter()
    response = (
        '```json\n'
        '{"results": [{"candidate": "军校劝退", "type": "risk_phrase", '
        '"category": "风险观点口号", "score": 70, "action": "observe", '
        '"match_type": "contains"}]}'
        '\n```'
    )
    keywords = adapter._parse_llm_response(response)

    assert len(keywords) == 1
    assert keywords[0].candidate == "军校劝退"
    assert keywords[0].score == 70


async def test_parse_empty_candidate_filtered():
    """候选词为 '空' 的条目被过滤"""
    adapter = XingpanAdapter()
    response = json.dumps({"results": [{"candidate": "空", "reason": "未发现候选词"}]})
    keywords = adapter._parse_llm_response(response)
    assert len(keywords) == 0


async def test_parse_empty_results_array():
    """空 results 数组返回空列表"""
    adapter = XingpanAdapter()
    response = json.dumps({"results": []})
    keywords = adapter._parse_llm_response(response)
    assert len(keywords) == 0
