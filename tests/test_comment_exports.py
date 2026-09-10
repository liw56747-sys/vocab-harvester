"""双平台各 100 帖：评论必须同时出现在 CSV、XLSX 与统计中。"""
import base64
import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from src.crawlers.comment_results import search_export_rows


def test_export_list_and_json_replies_preserve_parent_and_keyword():
    for raw in [[{'tweet_id': 'c', 'content': '长评论', 'author': '@bob'}],
                json.dumps([{'tweet_id': 'c', 'content': '长评论', 'author': '@bob'}])]:
        rows = search_export_rows([{'platform': 'twitter', 'tweet_id': 'p',
                                   'keyword': '测试', 'replies_data': raw}])
        assert len(rows) == 2
        assert rows[1]['parent_id'] == 'p'
        assert rows[1]['keyword'] == '测试'
        assert rows[1]['commenter'] == '@bob'
        assert rows[1]['url'] == 'https://x.com/i/status/c'


@pytest.mark.parametrize('endpoint', ['/api/search', '/api/batch-search'])
@pytest.mark.parametrize('include_replies', [True, False])
@pytest.mark.parametrize('available_count', [100, 37])
def test_dual_platform_100_posts_speed_mode_exports_all_comments(monkeypatch, endpoint, include_replies, available_count):
    from src.api.main import app
    from src.crawlers.twitter_url import TwitterCookieFetcher
    from src.crawlers.reddit_crawler import RedditCookieFetcher

    calls = []
    async def twitter(self, keyword, count, include_replies, **kwargs):
        calls.append(('twitter', count, include_replies, self.block_resources))
        rows = []
        for i in range(min(count, available_count)):
            rows.append({'tweet_id': f't{i}', 'type': 'post', 'content': 'X 主帖',
                         'replies_data': json.dumps([{'tweet_id': f't{i}c', 'content': 'X 评论',
                                                     'display_name': '甲'}] if include_replies else []),
                         'replies_count': int(include_replies)})
        if include_replies:
            rows[0]['comments_warning'] = '部分评论加载失败（测试）'
        return rows, ''

    async def reddit(self, keyword, count, include_replies, **kwargs):
        calls.append(('reddit', count, include_replies, True))
        posts = [{'post_id': f'r{i}', 'type': 'post', 'title': 'Reddit 标题', 'content': '正文'} for i in range(min(count, available_count))]
        comments = [{'post_id': f'r{i}', 'type': 'comment', 'content': 'Reddit 评论',
                     'commenter': 'u/乙', 'parent_id': f'r{i}'} for i in range(min(count, available_count))] if include_replies else []
        return posts + comments, ''

    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', twitter)
    monkeypatch.setattr(RedditCookieFetcher, 'search_posts', reddit)
    request = {'count': 100, 'platforms': ['twitter', 'reddit'], 'include_replies': include_replies,
               'block_resources': True, 'cookies': [
                   {'platform': 'twitter', 'ct0': 'fake', 'auth_token': 'fake'},
                   {'platform': 'reddit', 'reddit_session': 'fake'}]}
    request.update({'keywords': ['test'], 'stagger_platforms': False} if 'batch' in endpoint else {'keyword': 'test'})
    client = TestClient(app)
    start = client.post(endpoint, json=request)
    assert start.status_code == 200
    task_id = start.json()['task_id']
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get('/api/task-status', params={'task_id': task_id}).json()
        if state['status'] != 'running':
            break
        time.sleep(0.01)
    assert state['status'] == 'success', state
    result = state['result']
    assert sorted(calls) == [('reddit', 100, include_replies, True), ('twitter', 100, include_replies, True)]
    assert result['total_posts'] == available_count * 2
    assert len(result['count_results']) == 2
    for outcome in result['count_results']:
        assert outcome['requested_count'] == 100
        assert outcome['actual_count'] == available_count
        assert outcome['missing_count'] == 100 - available_count
        assert outcome['status'] == ('fulfilled' if available_count == 100 else 'shortfall')
    assert result['total_rows'] == available_count * (4 if include_replies else 2)
    csv_rows = list(csv.DictReader(io.StringIO(base64.b64decode(result['csv_data']).decode('utf-8-sig').lstrip('\ufeff'))))
    workbook = load_workbook(io.BytesIO(base64.b64decode(result['xlsx_data'])))
    values = list(workbook.active.values)
    xlsx_rows = [dict(zip(values[0], row)) for row in values[1:]]
    assert len(csv_rows) == len(xlsx_rows) == result['total_rows']
    for rows in [csv_rows, xlsx_rows]:
        comments = [r for r in rows if r['type'] == 'comment']
        assert len(comments) == (available_count * 2 if include_replies else 0)
        if include_replies:
            assert {r['content'] for r in comments} == {'X 评论', 'Reddit 评论'}
            assert all(r['parent_id'] and r['commenter'] for r in comments)
    assert bool(result['errors']) == (include_replies or available_count < 100)
    if available_count < 100:
        assert any('目标 100 条主帖，实际 37 条，缺少 63 条' in e for e in result['errors'])
    if 'batch' in endpoint:
        assert result['keyword_results'][0]['total_rows'] == result['total_rows']
