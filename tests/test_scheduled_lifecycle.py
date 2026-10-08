"""真实线程/事件循环、临时数据库和文件：重入、心跳、取消与部分结果。"""
import asyncio
import csv
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openpyxl import load_workbook

import src.api.main as api
from src.common.database import get_db
from src.common.models import ParsedPost
from src.crawlers.browser_manager import BrowserManager, BrowserPool
from src.crawlers.progress import report_rows
from src.crawlers.scheduled_run import ScheduledRun
from src.crawlers.twitter_url import TwitterCookieFetcher


@pytest.fixture
async def scheduled(init_test_db, tmp_path, monkeypatch):
    task = {'id': 'lifecycle-task', 'name': '生命周期', 'task_type': 'search', 'save_path': str(tmp_path),
            'params': {'keywords': ['test'], 'platforms': ['twitter', 'reddit'], 'count': 5,
                       'include_replies': True, 'block_resources': True, 'analyze': False}}
    db = await get_db()
    now = datetime.now().isoformat()
    await db.execute('INSERT INTO scheduled_tasks (id,name,created_at,updated_at) VALUES (?,?,?,?)',
                     (task['id'], task['name'], now, now))
    await db.commit()
    monkeypatch.setattr(api, '_get_platform_cookies_db', AsyncMock(return_value={
        'twitter': {'ct0': 'fake', 'auth_token': 'fake'}, 'reddit': {'reddit_session': 'fake'}}))
    monkeypatch.setattr(api, '_push_scheduled_notification', Mock())
    monkeypatch.setattr(api, '_SCHED_POLL_SECONDS', 0.01)
    monkeypatch.setattr(api, '_SCHED_STOP_GRACE_SECONDS', 2)
    monkeypatch.setattr(BrowserManager, 'close', AsyncMock())
    yield task
    assert task['id'] not in api._scheduled_runs


async def test_all_entry_points_reject_duplicate_execution(scheduled, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def body(task, run):
        entered.set()
        await release.wait()
    execute = AsyncMock(side_effect=body)
    monkeypatch.setattr(api, '_execute_scheduled_task_body', execute)
    first = asyncio.create_task(api._execute_scheduled_task(scheduled))
    try:
        await entered.wait()
        await api._execute_scheduled_task(scheduled)  # cron/reload entry
        reply = await api.run_scheduled_task_now(scheduled['id'])
        assert reply['status'] == 'error'
        assert '重复' in reply['error']
        assert execute.await_count == 1
    finally:
        release.set()
        await first


async def test_reload_does_not_mark_live_old_task_failed(scheduled, monkeypatch):
    db = await get_db()
    await db.execute("UPDATE scheduled_tasks SET last_run_status='running',last_run_at=? WHERE id=?",
                     ((datetime.now() - timedelta(hours=2)).isoformat(), scheduled['id']))
    await db.commit()
    run = api._claim_scheduled_run(scheduled['id'])
    monkeypatch.setattr(api, '_task_scheduler', Mock(get_jobs=Mock(return_value=[])))
    try:
        await api._load_scheduled_jobs()
        row = await (await db.execute('SELECT last_run_status FROM scheduled_tasks WHERE id=?', (scheduled['id'],))).fetchone()
        assert row[0] == 'running'
    finally:
        api._release_scheduled_run(scheduled['id'], run)


def test_browser_manager_is_owned_by_event_loop_and_thread():
    barrier = threading.Barrier(2)
    def worker():
        async def check():
            manager = BrowserManager.get()
            assert manager is BrowserManager.get()
            assert manager._pool is BrowserPool.get()
            barrier.wait(timeout=3)
            return manager, asyncio.get_running_loop()
        return asyncio.run(check())
    with ThreadPoolExecutor(2) as executor:
        one, two = executor.submit(worker), executor.submit(worker)
        a, b = one.result(timeout=5), two.result(timeout=5)
    assert a[0] is not b[0]
    assert a[0]._pool is not b[0]._pool
    async def manager():
        return BrowserManager.get()
    assert asyncio.run(manager()) is not asyncio.run(manager())


@pytest.mark.parametrize('save_format', ['csv', 'xlsx'])
@pytest.mark.parametrize('reddit_first', [False, True])
async def test_watchdog_cancels_real_worker_and_saves_partial_results(scheduled, monkeypatch, tmp_path, save_format, reddit_first):
    import src.crawlers.real_crawler as real
    scheduled['params']['save_format'] = save_format
    if reddit_first:
        scheduled['params']['platforms'] = ['reddit', 'twitter']
    cancelled = threading.Event()
    async def search(self, *args, **kwargs):
        # 使用生产增量映射链路，不能用直接返回模拟结果绕过取消。
        report_rows([{'tweet_id': 'post1', 'content': '已取得的主帖',
                      'replies_data': [{'tweet_id': 'reply1', 'content': '已取得的评论'}]}])
        run = api._scheduled_runs[scheduled['id']]
        with run._lock:
            run.last_progress -= 3000  # 模拟后续无进展，无需真正等待 40 分钟。
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', search)
    reddit = AsyncMock(return_value=[ParsedPost(platform='reddit', post_id='rd', author='test', content='另一平台数据',
        published_at=datetime.now(), raw_data={'type': 'post'})])
    monkeypatch.setattr(real, 'crawl_reddit', reddit)
    await asyncio.wait_for(api._execute_scheduled_task(scheduled), timeout=5)
    assert cancelled.is_set()
    assert reddit.await_count == int(reddit_first)
    db = await get_db()
    row = await (await db.execute('SELECT last_run_status,last_error FROM scheduled_tasks WHERE id=?', (scheduled['id'],))).fetchone()
    assert row[0] == 'partial'
    assert '抓取已停止' in row[1]
    if not reddit_first:
        assert '尚未执行' in row[1]
    files = list(tmp_path.glob('*.' + save_format))
    assert len(files) == 1
    if save_format == 'csv':
        with files[0].open(encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
    else:
        wb = load_workbook(files[0]); values = list(wb.active.values); wb.close()
        rows = [dict(zip(values[0], value)) for value in values[1:]]
    contents = {r['content'] for r in rows}
    assert {'已取得的主帖', '已取得的评论'} <= contents
    assert ('另一平台数据' in contents) == reddit_first
    assert next(r for r in rows if r['content'] == '已取得的评论')['parent_id'] == 'post1'


async def test_unresponsive_worker_is_not_reported_stopped_and_keeps_guard(scheduled, monkeypatch):
    release = threading.Event()
    def stuck(*args):
        run = args[-1]
        run.start_platform('twitter')
        with run._lock:
            run.last_progress -= 3000
        release.wait(timeout=5)
        return run.result(['twitter', 'reddit'], stopped=True)
    monkeypatch.setattr(api, '_scheduled_real_crawl', stuck)
    monkeypatch.setattr(api, '_SCHED_STOP_GRACE_SECONDS', 0.01)
    try:
        await api._execute_scheduled_task(scheduled)
        run = api._scheduled_runs[scheduled['id']]
        assert not run.future.done()
        db = await get_db()
        row = await (await db.execute('SELECT last_error FROM scheduled_tasks WHERE id=?', (scheduled['id'],))).fetchone()
        assert '停止尚未确认' in row[0]
        assert (await api.run_scheduled_task_now(scheduled['id']))['status'] == 'error'
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(run.future), timeout=3)
        await asyncio.sleep(0)


async def test_continuing_progress_can_run_beyond_total_stall_duration(scheduled, monkeypatch):
    import src.crawlers.real_crawler as real
    clock = [0.0]
    monkeypatch.setattr(ScheduledRun, '_clock', staticmethod(lambda: clock[0]))
    async def search(self, *args, **kwargs):
        for n in range(4):
            clock[0] += 1800  # 总时长两小时，每轮有数据，不能按总时长取消。
            report_rows([{'tweet_id': 'post1', 'content': '主帖', 'replies_data': [
                {'tweet_id': f'reply{i}', 'content': f'评论{i}'} for i in range(n + 1)]}])
            await asyncio.sleep(0.025)
        return [{'tweet_id': 'post1', 'content': '主帖'}], ''
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', search)
    monkeypatch.setattr(real, 'crawl_reddit', AsyncMock(return_value=[]))
    await api._execute_scheduled_task(scheduled)
    db = await get_db()
    row = await (await db.execute('SELECT last_run_status,last_error FROM scheduled_tasks WHERE id=?', (scheduled['id'],))).fetchone()
    assert tuple(row) == ('success', '')


async def test_scheduler_reload_keeps_existing_running_job(scheduled, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    monkeypatch.setattr(api, '_task_scheduler', scheduler)
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def job():
        entered.set()
        await release.wait()
        completed.set()
    scheduler.add_job(job, 'date', run_date=datetime.now(), id='active-probe')
    scheduler.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await api._load_scheduled_jobs()
        api._reload_scheduler()
        await asyncio.sleep(0.02)
        assert api._task_scheduler is scheduler
        assert not completed.is_set()
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=2)
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)



def test_snapshots_update_known_posts_without_erasing_saved_comments():
    from dataclasses import replace
    run = ScheduledRun()
    post = ParsedPost(platform='twitter', post_id='1', author='', content='正文',
                      published_at=datetime.now(), raw_data={'type': 'post'})
    comment = replace(post, post_id='2', content='已保存评论', raw_data={'type': 'comment', 'parent_id': '1'})
    run.save('twitter', [post, comment])
    run.save('twitter', [replace(post, author='updated-author')])
    run.finish_platform('twitter', [replace(post, author='updated-author')])
    rows = run.result()['posts']['twitter']
    assert len(rows) == 2
    assert rows[0].author == 'updated-author'
    assert rows[1].content == '已保存评论'
    run.cancel()
    run.save('twitter', [])
    assert len(run.result()['posts']['twitter']) == 2
