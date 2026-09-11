"""覆盖定时任务执行入口到文件落盘，外部平台返回固定数据。"""
import csv
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from openpyxl import load_workbook

import src.api.main as api
from src.common.database import get_db
from src.common.models import ParsedPost


@pytest.mark.parametrize('save_format', ['csv', 'xlsx'])
@pytest.mark.parametrize('stall_limit', [None, '600'])
async def test_scheduled_crawl_runs_and_saves_results(init_test_db, tmp_path, monkeypatch, save_format, stall_limit):
    if stall_limit is None:
        monkeypatch.delenv('VOCAB_SCHED_STALL_LIMIT', raising=False)
    else:
        monkeypatch.setenv('VOCAB_SCHED_STALL_LIMIT', stall_limit)
    task_id = 'schedule-execution-regression'
    db = await get_db()
    await db.execute(
        'INSERT INTO scheduled_tasks (id,name,created_at,updated_at,last_run_status,last_error) VALUES (?,?,?,?,?,?)',
        (task_id, '定时抓取测试', datetime.now().isoformat(), datetime.now().isoformat(), 'failed', "name 'os' is not defined"),
    )
    await db.commit()
    monkeypatch.setattr(api, '_get_platform_cookies_db', AsyncMock(return_value={
        'twitter': {'ct0': 'test', 'auth_token': 'test'},
        'reddit': {'reddit_session': 'test'},
    }))
    posts = {
        platform: [ParsedPost(platform=platform, post_id=f'{platform}-post', author='test', published_at=datetime.now(),
                              content=f'{platform} 主帖全文', raw_data={'type': 'post'})]
        for platform in ['twitter', 'reddit']
    }
    posts['twitter'].append(ParsedPost(platform='twitter', post_id='twitter-comment', author='reply', published_at=datetime.now(),
                                      content='评论全文', raw_data={'type': 'comment', 'parent_id': 'twitter-post'}))
    def crawl(targets, keywords, count, sort, include_replies, block_resources, seen_ids, heartbeat):
        assert [p for p, _, _ in targets] == ['twitter', 'reddit']
        assert keywords == ['测试'] and count == 100
        assert include_replies and block_resources
        assert seen_ids == set()
        heartbeat()
        return {'posts': posts, 'errors': {}}
    crawl_mock = Mock(side_effect=crawl)
    monkeypatch.setattr(api, '_scheduled_real_crawl', crawl_mock)
    notify = Mock()
    monkeypatch.setattr(api, '_push_scheduled_notification', notify)
    await api._execute_scheduled_task({
        'id': task_id, 'name': '定时抓取测试', 'task_type': 'search', 'save_path': str(tmp_path),
        'params': {'keywords': ['测试'], 'platforms': ['twitter', 'reddit'], 'count': 100,
                   'analyze': False, 'include_replies': True, 'block_resources': True, 'save_format': save_format},
    })
    row = await (await db.execute('SELECT last_run_status,last_error FROM scheduled_tasks WHERE id=?', (task_id,))).fetchone()
    assert tuple(row) == ('success', ''), tuple(row)
    crawl_mock.assert_called_once()
    files = list(tmp_path.glob('*.' + save_format))
    assert len(files) == 1
    if save_format == 'csv':
        with files[0].open(encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))
    else:
        wb = load_workbook(files[0])
        values = list(wb.active.values)
        rows = [dict(zip(values[0], row)) for row in values[1:]]
        wb.close()
    assert {row['content'] for row in rows} == {'twitter 主帖全文', 'reddit 主帖全文', '评论全文'}
    comment = next(row for row in rows if row['type'] == '评论')
    assert comment['parent_id'] == 'twitter-post'
    seen = await (await db.execute('SELECT post_id FROM scheduled_seen_posts')).fetchall()
    assert {row[0] for row in seen} == {'twitter-post', 'reddit-post'}
    assert notify.call_args.args[2] == 'success'
