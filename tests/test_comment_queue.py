"""Queue fairness, isolated workers, immediate durable deltas and recovery."""
import asyncio
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.collection import engine
from src.collection.comment_queue import CommentJob, drain_comments
from src.collection.comment_session import CommentSession
from src.collection.engine import CollectionSpec, Traffic
from src.collection.store import RowIndex, Store
from src.crawlers.comment_policy import managed_attempt
from src.crawlers.comment_state import comment_summary
from src.crawlers.platform_config import twitter_config
from src.crawlers.progress import report_rows
from src.crawlers.twitter_url import TwitterCookieFetcher
from tests.test_comment_collection import ReplyPage, no_wait
from tests.test_comment_efficiency import fake_browser, x_search


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR', str(tmp_path))
    Traffic.active.clear(); Traffic.delay.clear(); Traffic.until.clear()
    yield
    Traffic.active.clear(); Traffic.delay.clear(); Traffic.until.clear()


def parent(i):
    return dict(platform='twitter', type='post', tweet_id=str(i), post_id=str(i),
                keyword='kw', url=f'https://x.com/a/status/{i}', comments_status='pending')


async def test_budget_yields_before_retry_and_retains_partial_chunks():
    session = CommentSession(TwitterCookieFetcher(), 'twitter', {})
    index = RowIndex([parent(i) for i in range(6)])
    events = []; active = 0; peak = 0
    def save(rows): index.update(rows)
    jobs = [CommentJob(parent(i), save) for i in range(6)]
    async def fetch(post):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        try:
            events.append((post['tweet_id'], post['comments_round']))
            post['replies_data'] = [{'tweet_id':post['tweet_id'] + '-saved'}]
            report_rows([post])
            if post['tweet_id'] == '0' and post['comments_round'] == 1:
                await asyncio.Future()
            post.update(comments_status='finished', comments_reason='')
            return [post]
        finally:
            active -= 1
    await drain_comments(jobs, session, fetch, AsyncMock(), lambda *a: None, lambda: False,
                         replace(twitter_config, comment_pass_budgets=(.03, .1)))
    assert peak == 2 and active == 0
    assert events[-1] == ('0', 2)
    assert len([e for e in events if e[1] == 1]) == 6
    assert all(len(p['replies_data']) == 1 and p['comments_status'] == 'finished' for p in index.rows())
    assert all(not p['comments_active'] for p in index.rows())


async def test_all_keywords_are_covered_before_deep_retry(monkeypatch):
    spec = CollectionSpec(keywords=['a', 'b', 'c'], count=3, include_replies=True)
    store = Store(); rid = store.create(spec.model_dump(mode='json'))
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', x_search)
    events = []
    async def fetch(fetcher, platform, posts, cookies):
        p = posts[0]; events.append((p['keyword'], p['tweet_id'], p['comments_round']))
        p['replies_data'] = [{'tweet_id': p['tweet_id']+'-1'}]
        if p['tweet_id'] == 'a-0' and p['comments_round'] == 1:
            p.update(comments_status='partial', comments_reason='shortfall')
        else:
            p.update(comments_status='finished', comments_reason='')
        return posts
    monkeypatch.setattr(engine, 'fetch_comments', fetch)
    rows, info = await engine.collect(store, rid, {'twitter':{'ct0':'x','auth_token':'x'}})
    assert len(events) == 10 and events[-1] == ('a', 'a-0', 2)
    assert {e[0] for e in events[:9]} == {'a','b','c'}
    assert info['comments_pending'] == 0
    assert all(p['replies_data'][0]['tweet_id'].startswith(p['tweet_id']+'-') for p in rows)


async def test_no_new_shortfall_never_retries_in_same_run():
    session = CommentSession(TwitterCookieFetcher(), 'twitter', {})
    calls = []
    async def fetch(post):
        calls.append(1)
        post.update(comments_status='partial', comments_reason='shortfall')
        return [post]
    await drain_comments([CommentJob(parent(1), lambda rows: None)], session, fetch,
                         AsyncMock(), lambda *a: None, lambda: False, twitter_config)
    assert len(calls) == 1


async def test_user_cancel_joins_workers_before_releasing_gate():
    session = CommentSession(TwitterCookieFetcher(), 'twitter', {})
    active = 0; stopped = False; entered = asyncio.Event(); released = []
    async def fetch(post):
        nonlocal active
        active += 1; entered.set()
        try:
            await asyncio.Future()
        finally:
            active -= 1
    task = asyncio.create_task(drain_comments([CommentJob(parent(i), lambda rows: None) for i in range(10)],
        session, fetch, AsyncMock(), lambda *a: released.append(active), lambda: stopped, twitter_config))
    await entered.wait(); stopped = True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert active == 0 and released == [0]


async def test_transient_failure_reduces_concurrency_without_skipping_jobs():
    session = CommentSession(TwitterCookieFetcher(), 'twitter', {})
    seen = []
    async def fetch(post):
        seen.append((post['tweet_id'], post['comments_round']))
        if post['tweet_id'] == '0':
            post.update(comments_status='failed', comments_reason='network')
        else:
            post.update(comments_status='finished', comments_reason='')
        return [post]
    await drain_comments([CommentJob(parent(i), lambda rows: None) for i in range(9)], session,
                         fetch, AsyncMock(), lambda *a: None, lambda: False, twitter_config)
    assert {pid for pid, rnd in seen if rnd == 1} == {str(i) for i in range(9)}
    assert seen.count(('0', 2)) == 1 and ('0', 3) not in seen


async def test_context_not_recycled_while_other_page_is_active(monkeypatch):
    contexts = fake_browser(monkeypatch)
    session = CommentSession(TwitterCookieFetcher(), 'twitter', {})
    session.CONTEXT_POST_LIMIT = 1
    async with session.twitter_lease() as first:
        async with session.twitter_lease() as second:
            assert first is second and not first.closed
    async with session.twitter_lease() as third:
        assert third is not first and first.closed
    await session.close()
    assert len(contexts) == 2 and all(c.closed for c in contexts)


def test_delta_writes_do_not_rewrite_legacy_unit_and_survive_process_exit(tmp_path):
    path = tmp_path/'delta.db'
    store = Store(path); rid = store.create(CollectionSpec(keywords=['kw']).model_dump(mode='json'))
    p = parent(1); p['replies_data'] = [{'tweet_id':'old','content':'old'}]
    store.save(rid, 'unit', [p], {'target':1})
    with store.connect() as db:
        baseline = db.execute('select rows_json from units').fetchone()[0]
    script = '''import os,sys
from src.collection.store import Store
s=Store(sys.argv[1]);s.save_comment_delta(sys.argv[2], 'unit', [{'platform':'twitter','tweet_id':'1','comments_status':'partial','replies_data':[{'tweet_id':'new','content':'new'}]}]);os._exit(23)
'''
    proc = subprocess.run([sys.executable, '-c', script, str(path), rid])
    assert proc.returncode == 23
    with store.connect() as db:
        assert db.execute('select rows_json from units').fetchone()[0] == baseline
    reopened = Store(path); rows = reopened.rows(rid)
    assert {r['tweet_id'] for r in rows[0]['replies_data']} == {'old','new'}
    reopened.save_comment_delta(rid, 'unit', [{'platform':'twitter','tweet_id':'1','replies_data':[{'tweet_id':'new','content':'updated'}]}])
    rows = reopened.rows(rid)
    assert len(rows[0]['replies_data']) == 2
    assert next(r for r in rows[0]['replies_data'] if r['tweet_id']=='new')['content']=='updated'
    # A subsequent authoritative snapshot also compacts old deltas atomically.
    reopened.save(rid, 'unit', rows, {'target':1})
    assert reopened.rows(rid) == rows
    with reopened.connect() as db:
        assert db.execute('select count(*) from comment_updates').fetchone()[0] == 0
        assert db.execute('select count(*) from comment_replies').fetchone()[0] == 0


def test_row_index_retains_separate_parent_links_and_deduplicates_locally():
    index = RowIndex([parent(1), parent(2)])
    for _ in range(100):
        index.update([{**parent(1), 'replies_data':[{'tweet_id':'shared'}]},
                      {**parent(2), 'replies_data':[{'tweet_id':'shared'}]}])
    assert [len(r['replies_data']) for r in index.rows()] == [1,1]


async def test_clickable_more_without_new_comments_does_not_reset_stalls(no_wait):
    page = ReplyPage([])
    original = page.evaluate
    async def evaluate(script, arg=None):
        if script == TwitterCookieFetcher._CLICK_SHOW_MORE_REPLIES_JS:
            return True
        return await original(script, arg)
    page.evaluate = evaluate
    progress = {'replies':10}
    await TwitterCookieFetcher()._scrape_replies_page(page, page.url, progress=progress)
    assert page.rounds == twitter_config.comment_max_stalls
    assert progress['comments_status'] == 'partial'


async def test_managed_page_reports_only_changed_comments_and_attempts_once(no_wait):
    page = ReplyPage([[1,2], [3]])
    page.wait_for_function = AsyncMock()
    post = {'platform':'twitter', 'tweet_id':'999999', 'url':page.url, 'replies':100}
    chunks = []
    from src.crawlers.progress import observe_rows
    with managed_attempt(), observe_rows(lambda rows: chunks.extend(rows)):
        await TwitterCookieFetcher()._parallel_scrape_replies(SimpleNamespace(new_page=AsyncMock(return_value=page)), [post])
    assert post['comments_attempts'] == 1 and len(post['replies_data']) == 3
    sizes = [len(p.get('replies_data', [])) for p in chunks]
    assert sizes[:2] == [2,1]
    page.wait_for_function.assert_awaited()


def test_progress_states_partition_all_parents():
    rows = [parent(i) for i in range(5)]
    rows[1].update(comments_active=True)
    rows[2].update(comments_status='partial')
    rows[3].update(comments_status='failed')
    rows[4].update(comments_status='finished')
    s = comment_summary(rows)
    assert [s[k] for k in ['comments_unstarted','comments_in_progress','comments_partial','comments_failed','comments_finished']] == [1]*5


def test_concurrent_delta_writers_and_readers_keep_atomic_chunks():
    from concurrent.futures import ThreadPoolExecutor
    store = Store(); rid = store.create(CollectionSpec(keywords=['kw']).model_dump(mode='json'))
    store.save(rid, 'unit', [parent(i) for i in range(8)], {})
    def write(i):
        local = Store(store.path)
        for batch in range(20):
            local.save_comment_delta(rid, 'unit', [{**parent(i), 'comments_status':'partial',
                'replies_data':[{'tweet_id':f'{i}-{batch}-{j}','content':'saved'} for j in range(5)]}])
    def read():
        local = Store(store.path)
        for _ in range(20):
            rows = local.rows(rid)
            assert len(rows) == 8
            assert all(len(p.get('replies_data', [])) % 5 == 0 for p in rows)
    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(write, i) for i in range(8)] + [pool.submit(read)]
        for future in futures: future.result()
    rows = Store(store.path).rows(rid)
    assert all(len(p['replies_data']) == 100 for p in rows)
    assert all(all(r['tweet_id'].startswith(p['tweet_id']+'-') for r in p['replies_data']) for p in rows)
    with store.connect() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_failed_chunk_rolls_back_all_its_rows():
    store = Store(); rid = store.create(CollectionSpec(keywords=['kw']).model_dump(mode='json'))
    store.save(rid, 'unit', [parent(1)], {})
    with pytest.raises(TypeError):
        store.save_comment_delta(rid, 'unit', [{**parent(1), 'replies_data':[{'tweet_id':'good'}]},
                                              {**parent(2), 'content':object()}])
    rows = Store(store.path).rows(rid)
    assert len(rows) == 1 and not rows[0].get('replies_data')
