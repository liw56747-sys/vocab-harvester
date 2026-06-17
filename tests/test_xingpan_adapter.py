"""测试 XingpanAdapter 对不同格式的解析"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.adapter.xingpan import XingpanAdapter


async def test_parse_full_json():
    """测试解析完整 JSON 格式的 results 数组"""
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
                "match_type": "exact"
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
                "match_type": "context_only"
            },
            {
                "source_id": "",
                "candidate": "空",
                "reason": "未发现候选词"
            }
        ]
    })
    keywords = adapter._parse_llm_response(response)

    assert len(keywords) == 2  # "空" should be filtered out

    kw1 = keywords[0]
    assert kw1.candidate == "避坑军校"
    assert kw1.type == "event_word"
    assert kw1.category == "事件核心词"
    assert kw1.score == 85
    assert kw1.action == "need_human_review"
    assert kw1.match_type == "exact"
    print("[PASS] full JSON parse OK")


async def test_parse_markdown_wrapped():
    """测试解析 markdown 代码块包裹的 JSON"""
    adapter = XingpanAdapter()
    response = '```json\n{"results": [{"candidate": "军校劝退", "type": "risk_phrase", "category": "风险观点口号", "score": 70, "action": "observe", "match_type": "contains"}]}\n```'
    keywords = adapter._parse_llm_response(response)

    assert len(keywords) == 1
    assert keywords[0].candidate == "军校劝退"
    assert keywords[0].score == 70
    print("[PASS] markdown-wrapped JSON parse OK")


async def test_parse_candidate_score_string():
    """测试解析 'candidate：score' 格式"""
    from src.common.models import WorkflowResult

    text = "避坑军校：85；军校发配：75；劝退军校：60"
    result = WorkflowResult.from_candidate_score_string(text, "test_task_2")

    assert result.task_id == "test_task_2"
    assert len(result.keywords) == 3

    assert result.keywords[0].candidate == "避坑军校"
    assert result.keywords[0].score == 85
    assert result.keywords[1].candidate == "军校发配"
    assert result.keywords[1].score == 75
    assert result.keywords[2].candidate == "劝退军校"
    assert result.keywords[2].score == 60
    print("[PASS] candidate:score string parse OK")


async def test_parse_empty():
    """测试空结果"""
    adapter = XingpanAdapter()
    response = json.dumps({"results": [{"candidate": "空", "reason": "未发现候选词"}]})
    keywords = adapter._parse_llm_response(response)

    assert len(keywords) == 0
    print("[PASS] empty result parse OK")


async def test_parse_empty_results():
    """测试空 results 数组"""
    adapter = XingpanAdapter()
    response = json.dumps({"results": []})
    keywords = adapter._parse_llm_response(response)

    assert len(keywords) == 0
    print("[PASS] empty results array parse OK")


async def test_parse_string_empty():
    """测试空字符串"""
    from src.common.models import WorkflowResult

    result = WorkflowResult.from_candidate_score_string("空", "test_task_4")
    assert len(result.keywords) == 0
    print("[PASS] empty string parse OK")


async def main():
    await test_parse_full_json()
    await test_parse_markdown_wrapped()
    await test_parse_candidate_score_string()
    await test_parse_empty()
    await test_parse_empty_results()
    await test_parse_string_empty()
    print("\n[ALL TESTS PASSED]")


if __name__ == "__main__":
    asyncio.run(main())
