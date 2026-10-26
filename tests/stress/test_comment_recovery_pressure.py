"""Bounded load and fault recovery for comment backfill, with no live platform traffic."""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx
import pytest

from src.collection import api as collection_api, engine
from src.collection.store import Store
from src.collection.engine import CollectionSpec
from src.crawlers.comment_state import comment_summary
from src.crawlers.comment_results import search_export_rows
from src.api import main as api


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(engine.Traffic, 'enter', AsyncMock())
    monkeypatch.setattr(engine.Traffic, 'leave', lambda *a, **kw: None)


def seed(store):
    spec = CollectionSpec(keywords=['load'], platforms=['twitter','reddit'], count=50, include_replies=True)
    rid = store.create(spec.model_dump(mode='json'))
    for platform in spec.platforms:
        rows = []
        for i in range(50):
            row = {'platform': platform, 'type': 'post', 'post_id': str(i), 'keyword': 'load',
                   'comments_status': 'finished' if i < (13 if platform == 'twitter' else 12) else 'failed',
                   'num_comments': 10, 'content': 'parent'}
            if platform == 'twitter':
                row.update(tweet_id=str(i), replies_data=[{'tweet_id': f'{i}-{n}', 'content': 'retained'*20} for n in range(60)])
            rows.append(row)
        uid = hashlib.sha256((platform+'\0load').encode()).hexdigest()[:24]
        store.save(rid, uid, rows, {'search_finished': True, 'target': 50, 'platform': platform})
    return rid


async def test_100_parents_3000_comments_two_recoveries_and_restart(monkeypatch):
    store = Store(); rid = seed(store)
    calls = []
    async def comments(fetcher, platform, posts, cookies):
        post = posts[0]; ident = post.get('tweet_id') or post['post_id']
        calls.append((platform, ident))
        if platform == 'reddit' and int(ident) >= 45:
            raise RuntimeError('404 unavailable')
        post['comments_status'] = 'finished'
        post['comments_warning'] = ''
        post['comments_reason'] = ''
        if platform == 'twitter':
            post['replies_data'] = [{'tweet_id': f'{ident}-new-{n}'} for n in range(10)]
            return [post]
        return [post] + [{'platform': 'reddit', 'type': 'comment', 'comment_id': f'{ident}-{n}',
                          'parent_id': ident, 'content': 'new'} for n in range(10)]
    monkeypatch.setattr(engine, 'fetch_comments', comments)
    cookies = {'twitter': {'ct0':'test','auth_token':'test'}, 'reddit': {'reddit_session':'test'}}
    rows, detail = await engine.collect(store, rid, cookies, comments_only=True)
    assert len(calls) == 75 and detail['comments_pending'] == 5
    progress = Store(store.path).progress(rid)
    assert progress['total_comments'] == 3700 and progress['new_comments'] == 700
    assert progress['comments_unavailable'] == 5
    assert progress['comments_finished'] + progress['comments_pending'] == 100
    assert len(search_export_rows(rows)) == 3800
    async def recovered(fetcher, platform, posts, cookies):
        assert platform == 'reddit' and int(posts[0]['post_id']) >= 45
        p = posts[0]; p.update(comments_status='finished', comments_reason='', comments_warning='')
        return [p] + [{'platform': 'reddit', 'type': 'comment', 'comment_id': f"{p['post_id']}-{i}", 'parent_id': p['post_id']} for i in range(10)]
    monkeypatch.setattr(engine, 'fetch_comments', recovered)
    restored = Store(store.path)
    rows, detail = await engine.collect(restored, rid, cookies, comments_only=True)
    assert detail['status'] == 'success' and detail['comments_pending'] == 0
    assert restored.progress(rid)['new_comments'] == 50
    assert restored.progress(rid)['total_comments'] == 3750
    await engine.collect(restored, rid, cookies, comments_only=True)
    assert restored.progress(rid)['new_comments'] == 0
    assert restored.progress(rid)['total_comments'] == 3750


async def test_300_summary_requests_during_checkpoint_writes(monkeypatch):
    store = Store(); rid = seed(store)
    initial = comment_summary(store.rows(rid))
    store.progress(rid, initial); store.status(rid, 'running')
    monkeypatch.setattr(collection_api, 'run_active', lambda _: True)
    monkeypatch.setattr(api, '_generate_xlsx_bytes', lambda *a, **kw: pytest.fail('Polling generated Excel'))
    monkeypatch.setattr(Store, 'rows', lambda *a, **kw: pytest.fail('Polling reread raw comments'))
    def writer():
        local = Store(store.path)
        for i in range(100):
            local.progress(rid, {**initial, 'comments_finished': 25+i%76, 'comments_pending': 75-i%76,
                                 'new_comments': i, 'total_comments': 3000+i})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://test') as client:
            limit = asyncio.Semaphore(20)
            async def read(i):
                async with limit:
                    response = await client.get('/api/collections' if i%2 else f'/api/collections/{rid}/summary')
                    assert response.status_code == 200
                    data = response.json(); state = data['runs'][0] if 'runs' in data else data
                    assert state['status'] == 'running'
                    assert state['comments_finished']+state['comments_pending'] == 100
                    assert len(response.content) < 4096
            await asyncio.gather(*(read(i) for i in range(300)))
        future.result()
    with store.connect() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


async def test_cancel_backfill_preserves_first_checkpoint_and_progress(monkeypatch):
    store = Store(); rid = seed(store)
    entered = asyncio.Event()
    async def comments(fetcher, platform, posts, cookies):
        posts[0].update(comments_status='partial', comments_reason='network',
                        replies_data=[{'tweet_id':'cancel-saved','content':'must survive cancellation'}])
        from src.crawlers.progress import report_rows
        report_rows(posts)
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(engine, 'fetch_comments', comments)
    cookies = {'twitter':{'ct0':'test','auth_token':'test'}, 'reddit':{'reddit_session':'test'}}
    task = asyncio.create_task(engine.collect(store, rid, cookies, comments_only=True))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    reopened = Store(store.path)
    assert reopened.get(rid)['status'] == 'interrupted'
    assert reopened.progress(rid)['phase'] == 'interrupted'
    assert reopened.progress(rid)['total_comments'] == 3002
    assert reopened.progress(rid)['comments_in_progress'] == 0
    assert any(r.get('post_id') == 'cancel-saved' for r in search_export_rows(reopened.rows(rid)))
