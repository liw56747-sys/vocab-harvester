"""Release pressure/fault tests; isolated databases and synthetic platform data only."""
import asyncio
import base64
import csv
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from openpyxl import load_workbook
from src.api import main as api
from src.collection import api as collection_api
from src.collection.engine import CollectionSpec, Traffic
from src.collection.store import Store, key

@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR', str(tmp_path))


def test_concurrent_checkpoints_and_watermarks(tmp_path):
    store = Store(tmp_path/'pressure.db')
    spec = CollectionSpec(keywords=['pressure']).model_dump(mode='json')
    ids = [store.create(spec) for _ in range(16)]
    def write(index):
        local = Store(store.path)
        for step in range(30):
            rows = [{'platform':'twitter','tweet_id':str(n),'content':'压力测试' * 20} for n in range(step+1)]
            local.save(ids[index], 'unit', rows, {'step':step})
            local.watermark('shared', f'2026-10-{step+1:02d}T00:00:00+00:00')
            assert len(local.rows(ids[index])) == step+1
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(write, range(16)))
    with store.connect() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert all(len(store.rows(rid))==30 for rid in ids)
    assert store.watermark('shared')=='2026-10-30T00:00:00+00:00'


def seed_large(store, count=10000):
    rid=store.create(CollectionSpec(keywords=['large'],count=count,include_replies=True).model_dump(mode='json'))
    rows=[{'platform':'twitter','keyword':'large','tweet_id':str(i),'content':f'{i} 中文\n"quoted",😀',
           'comments_status':'finished','replies_data':[{'tweet_id':f'{i}-r{j}','content':f'评论 {j}'} for j in range(3)]} for i in range(count)]
    store.save(rid,'large',rows,{'platform':'twitter','keyword':'large','target':count})
    store.status(rid,'success')
    return rid


def test_large_export_integrity_and_dedup():
    store=Store();rid=seed_large(store)
    output=collection_api.result(store,rid)
    rows=list(csv.DictReader(io.StringIO(base64.b64decode(output['csv_data']).decode('utf-8-sig'))))
    assert len(rows)==40000
    assert output['total_posts']==10000 and output['total_comments']==30000
    assert len({(r['type'],r['post_id']) for r in rows})==40000
    assert rows[-1]['parent_id']=='9999'
    wb=load_workbook(io.BytesIO(base64.b64decode(output['xlsx_data'])),read_only=True)
    assert sum(1 for _ in wb.active.rows)==40001
    wb.close()


async def test_export_does_not_block_event_loop(monkeypatch):
    store=Store();rid=seed_large(store,100)
    original=api._generate_xlsx_bytes
    def slow_export(*args,**kwargs):
        time.sleep(.25)
        return original(*args,**kwargs)
    monkeypatch.setattr(api,'_generate_xlsx_bytes',slow_export)
    gaps=[];stop=False
    async def heartbeat():
        while not stop:
            start=time.monotonic()
            await asyncio.sleep(.01)
            gaps.append(time.monotonic()-start)
    timer=asyncio.create_task(heartbeat())
    await asyncio.sleep(.02)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
            response=await client.get(f'/api/collections/{rid}/result')
            assert response.status_code==200
        await asyncio.sleep(.02)
    finally:
        stop=True;await timer
    assert max(gaps)<.15, f'export blocked event loop for {max(gaps):.3f}s'


async def test_submission_storm_has_bounded_workers(init_test_db,monkeypatch):
    release=threading.Event()
    async def blocked_collect(*args,**kwargs):
        while not release.is_set():
            await asyncio.sleep(.01)
        args[0].status(args[1],'success')
    monkeypatch.setattr(collection_api,'collect',blocked_collect)
    ids=[]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
            responses=await asyncio.gather(*[client.post('/api/search',json={'keyword':f'pressure-{i}','count':1}) for i in range(24)])
            ids=[r.json()['task_id'] for r in responses if r.status_code==200]
            assert len(ids)<=8, f'unbounded workers: {len(ids)}'
            assert sum(r.status_code==429 for r in responses)==24-len(ids)
    finally:
        release.set()
        for _ in range(500):
            if not collection_api._active:break
            await asyncio.sleep(.02)
        assert not collection_api._active


async def test_cross_loop_gate_200_acquisitions_and_cancel():
    Traffic.active.clear();Traffic.until.clear();Traffic.delay.clear()
    active=set();lock=threading.Lock();peaks=[]
    async def execute(index):
        platform=['twitter','reddit'][index%2]
        for _ in range(10):
            await Traffic.enter(platform,lambda:False)
            try:
                with lock:
                    assert platform not in active
                    active.add(platform);peaks.append(len(active))
                await asyncio.sleep(.001)
            finally:
                with lock:active.remove(platform)
                Traffic.leave(platform)
    def worker(i):asyncio.run(execute(i))
    await asyncio.to_thread(lambda: list(ThreadPoolExecutor(max_workers=20).map(worker,range(20))))
    assert len(peaks)==200 and max(peaks)<=2 and not Traffic.active


async def test_resume_storm_does_not_reset_active_checkpoints(init_test_db,monkeypatch):
    store=Store();rid=store.create(CollectionSpec(keywords=['resume']).model_dump(mode='json'))
    store.save(rid,'unit',[],{'search_finished':True,'partition':9})
    release=threading.Event()
    async def blocked(*args,**kwargs):
        while not release.is_set():await asyncio.sleep(.01)
    monkeypatch.setattr(collection_api,'collect',blocked)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
            replies=await asyncio.gather(*[client.post(f'/api/collections/{rid}/resume') for _ in range(40)])
            assert sum(r.status_code==200 for r in replies)==1
            assert sum(r.status_code==409 for r in replies)==39
            store.save(rid,'unit',[],{'search_finished':True,'partition':7})
            assert (await client.post(f'/api/collections/{rid}/resume')).status_code==409
            assert store.units(rid)['unit'][1]['partition']==7
    finally:
        release.set()
        for _ in range(500):
            if rid not in collection_api._active:break
            await asyncio.sleep(.01)
        assert rid not in collection_api._active


async def test_simultaneous_missed_dispatch_is_idempotent(init_test_db,monkeypatch):
    import json
    from datetime import datetime,timedelta,timezone
    from unittest.mock import AsyncMock
    from src.common.database import get_db
    from src.collection import scheduler
    db=await get_db();now=datetime.now(timezone.utc)
    params={'timezone':'Asia/Shanghai','cron_dialect':'unix','missed_policy':'once'}
    configs=[{'id':f'due-{i}','params':params} for i in range(30)]
    for c in configs:
        await db.execute('INSERT INTO scheduled_tasks(id,params,cron_expression,created_at,updated_at) VALUES(?,?,?,?,?)',
                         (c['id'],json.dumps(params),'* * * * *',now.isoformat(),now.isoformat()))
        await scheduler.register(db,c,'* * * * *')
    await db.execute('UPDATE schedule_state SET next_due=?',((now-timedelta(minutes=10)).isoformat(),));await db.commit()
    run=AsyncMock();monkeypatch.setattr(api,'_execute_scheduled_task',run)
    await asyncio.gather(*[scheduler.dispatch(c,late_only=True) for c in configs for _ in range(10)])
    assert run.await_count==30
    assert len({call.args[0]['id'] for call in run.call_args_list})==30


async def test_scheduled_worker_storm_is_bounded(monkeypatch):
    active=0;peak=0;completed=[]
    async def body(config,run):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        await asyncio.sleep(.005)
        active-=1;completed.append(config['id'])
    monkeypatch.setattr(api,'_execute_scheduled_task_body',body)
    await asyncio.gather(*[api._execute_scheduled_task({'id':f'queue-{i}'}) for i in range(80)])
    assert peak<=4 and len(set(completed))==80
    assert not api._scheduled_runs


async def test_ten_dual_platform_runs_with_comments_recovery_and_export(monkeypatch):
    from src.collection import engine
    from src.crawlers.progress import report_rows
    from src.crawlers.twitter_url import TwitterCookieFetcher
    from src.crawlers.reddit_crawler import RedditCookieFetcher
    calls=[]
    async def search(self,keyword,count,**kwargs):
        calls.append(keyword)
        rows=[{'tweet_id':str(i),'post_id':str(i),'content':f'content {i}','created_at':'2026-10-15T00:00:00Z'} for i in range(count)]
        for start in range(0,count,20):report_rows(rows[:start+20])
        return rows,''
    async def comments(fetcher,platform,posts,cookies):
        post=posts[0]
        post['comments_status']='finished'
        if platform=='twitter':
            post['replies_data']=[{'tweet_id':f"{post['tweet_id']}-c{j}",'content':f'reply {j}'} for j in range(3)]
            updated=posts
        else:
            updated=posts+[{'type':'comment','comment_id':f"{post['post_id']}-c{j}",'post_id':post['post_id'],
                'parent_id':post['post_id'],'content':f'reply {j}'} for j in range(3)]
        report_rows(updated)
        return updated
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',search)
    monkeypatch.setattr(engine,'fetch_comments',comments)
    Traffic.until.clear();Traffic.delay.clear()
    store=Store();cookies={'twitter':{'ct0':'c','auth_token':'a'},'reddit':{'reddit_session':'r'}}
    for index in range(10):
        rid=store.create(CollectionSpec(keywords=[f'round-{index}'],platforms=['twitter','reddit'],count=100,include_replies=True,block_resources=True).model_dump(mode='json'))
        rows,info=await engine.collect(store,rid,cookies)
        assert info['status']=='success' and len(rows)==500 and info['comments_pending']==0
        assert len({key(row) for row in rows})==500
        restored,info=await engine.collect(Store(store.path),rid,cookies,comments_only=True)
        assert len(restored)==500 and info['comments_pending']==0
        output=collection_api.result(store,rid)
        assert output['total_rows']==800 and output['total_comments']==600
        exported=list(csv.DictReader(io.StringIO(base64.b64decode(output['csv_data']).decode('utf-8-sig'))))
        assert sum(bool(row['comment_id']) for row in exported if row['platform']=='reddit')==300
    assert len(calls)==20


async def test_completed_download_cache_is_bounded_and_rebuilds(monkeypatch):
    from collections import OrderedDict
    monkeypatch.setattr(api,'_BACKGROUND_TASKS',OrderedDict())
    monkeypatch.setattr(api,'_BG_EXPORT_CACHE_BYTES',4000)
    store=Store();rid=seed_large(store,10)
    original=collection_api.result(store,rid)
    api._set_task_status(rid,{'status':'success','result':original})
    for i in range(100):
        api._set_task_status(str(i),{'status':'success','result':{'run_id':str(i),'csv_data':'x'*1000,'xlsx_data':'y'*1000}})
    cache=list(api._BACKGROUND_TASKS.values())
    assert sum(len((e.get('result') or {}).get('csv_data',''))+len((e.get('result') or {}).get('xlsx_data','')) for e in cache)<=4000
    assert api._get_task_status(rid)['_export_on_demand']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
        restored=(await client.get('/api/task-status',params={'task_id':rid})).json()
    assert restored['result']['csv_data']==original['csv_data']
    assert restored['result']['total_rows']==40
    assert 'csv_data' not in api._get_task_status(rid)['result']
