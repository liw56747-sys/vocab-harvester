"""Desktop API integration; lists return summaries only, export is explicit."""
import asyncio
import base64
import csv
import io
import threading
from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, HTTPException, Query
from src.collection.store import Store
from src.collection.engine import CollectionSpec, collect, expected_posts
from src.crawlers.comment_results import search_export_rows

router = APIRouter()
_active = set()
_lock = threading.Lock()
_MAX_ACTIVE = 8
_export_slots = threading.BoundedSemaphore(2)


def result(store, rid, detail=None):
    # Bound simultaneous large workbook allocations across workers and HTTP downloads.
    with _export_slots:
        return _result(store, rid, detail)


def _result(store, rid, detail=None):
    from src.api.main import _generate_xlsx_bytes
    record = store.get(rid)
    rows = store.rows(rid)
    exported = search_export_rows(rows)
    fields = ['keyword','search_query','platform','type','post_id','comment_id','parent_id','author','commenter','content','created_at','url',
              'likes','retweets','score','num_comments','media_urls','comments_status','comments_warning','collection_kind']
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(exported)
    spec = CollectionSpec(**record['spec'])
    main = [r for r in rows if r.get('type','post')!='comment']
    new = [r for r in main if r.get('collection_kind')!='revisit']
    expected = expected_posts(spec)
    counts = []
    for uid, (unit_rows, state) in store.units(rid).items():
        if uid.startswith('revisit:'):
            continue
        posts = [r for r in unit_rows if r.get('type','post')!='comment']
        if posts or state.get('platform'):
            n = len(posts)
            target = state.get('target', spec.count)
            counts.append({'platform':state.get('platform') or posts[0]['platform'],'keyword':state.get('keyword') or posts[0]['keyword'],
                           'actual_count':n,'requested_count':target,'missing_count':max(0,target-n), 'status':'fulfilled' if n>=target else 'shortfall'})
    pending = sum(r.get('comments_status')!='finished' for r in main) if spec.include_replies else 0
    return {**(detail or {}), 'run_id':rid, 'status':'cancelled' if record['status']=='interrupted' else 'success' if record['status']=='success' else 'partial',
            'count_scope':spec.count_scope, 'requested_posts':expected,'missing_posts':0 if spec.probe else max(0,expected-len(new)),
            'preflight':spec.probe,
            'total_posts':len(main), 'new_posts':len(new), 'refreshed_posts':len(main)-len(new),
            'total_rows':len(exported),'total_comments':len(exported)-len(main),
            'comments_pending':pending,'comments_finished':len(main)-pending if spec.include_replies else 0,
            'errors':[record['error']] if record['error'] else [], 'count_results':counts,
            'platform_counts':{p:sum(r['platform']==p for r in main) for p in spec.platforms},
            'keyword_results':[{'keyword':kw,'post_count':sum(r.get('keyword')==kw for r in main),'total_rows':sum(r.get('keyword')==kw for r in exported)} for kw in spec.keywords],
            'sampled_posts':[], 'csv_data':base64.b64encode(buf.getvalue().encode('utf-8-sig')).decode(),
            'csv_filename':f'collection_{rid[:12]}.csv',
            'xlsx_data':base64.b64encode(_generate_xlsx_bytes(exported,fields,'采集结果')).decode(),
            'xlsx_filename':f'collection_{rid[:12]}.xlsx'}


async def start(spec, supplied_cookies=None, rid=None, comments_only=False, reset_search=False):
    from src.api import main as api
    store = Store()
    if rid:
        try:
            spec = CollectionSpec(**store.get(rid)['spec'])
        except ValueError as e:
            raise HTTPException(404,str(e))
    with _lock:
        if rid in _active:
            raise HTTPException(409,'任务正在运行，不能重复启动')
        if len(_active) >= _MAX_ACTIVE:
            raise HTTPException(429, '正在进行的采集任务过多，请等待任务完成后重试')
        if not rid:
            rid = store.create(spec.model_dump(mode='json'))
        _active.add(rid)
    try:
        if reset_search:
            for unit, (rows, state) in store.units(rid).items():
                state.pop('search_finished', None)
                if not state.get('error'):
                    state['partition'] = 0
                store.save(rid, unit, rows, state)
        try:
            cookies = await api._get_platform_cookies_db()
        except RuntimeError:
            cookies = {}
        for config in supplied_cookies or []:
            config = config.model_dump() if hasattr(config,'model_dump') else dict(config)
            platform = config.pop('platform')
            # Do not overwrite a saved account with empty form fields.
            if any(config.get(k) for k in ('ct0','auth_token','reddit_session')):
                cookies[platform] = config
                await api._auto_persist_cookies(**{platform:config})
        store.status(rid,'running')
        api._set_task_status(rid,{'status':'running','result':None,'progress':{'total':0,'completed':0,'percent':0}})
        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            async def execute():
                task = asyncio.create_task(collect(store,rid,cookies,comments_only=comments_only,
                    cancelled=lambda:api._is_task_cancelled(rid),
                    progress=lambda p:api._set_task_status(rid,{'status':'running','progress':p,'result':None})))
                try:
                    while not task.done():
                        await asyncio.wait({task},timeout=.5)
                        if api._is_task_cancelled(rid):
                            task.cancel()
                    await task
                except asyncio.CancelledError:
                    store.status(rid,'interrupted','已取消，已保存检查点，可继续抓取')
                except Exception:
                    store.status(rid,'failed','采集异常，已保存检查点；请查看日志或续抓')
                    api.logger.exception('采集运行 %s 失败',rid)
                finally:
                    try:
                        api._set_task_status(rid,{'status':'success','result':result(store,rid)})
                    except Exception as exc:
                        api._set_task_status(rid,{'status':'error','error':str(exc)})
            try:
                loop.run_until_complete(execute())
            finally:
                try:
                    api._close_crawl_loop(loop)
                finally:
                    api._remove_cancelled(rid)
                    with _lock:
                        _active.discard(rid)
        threading.Thread(target=worker,name='collection-'+rid[:12],daemon=True).start()
        return {'status':'started','task_id':rid}
    except BaseException:
        with _lock:
            _active.discard(rid)
        raise


@router.get('/api/collections')
def history(offset: int = Query(0,ge=0)):
    store = Store()
    runs = store.list(offset=offset)
    with _lock:
        active = set(_active)
    for r in runs:
        r['active'] = r['id'] in active
        if r['status']=='running' and not r['active']:
            # Scheduled runs are tracked separately by their collection run id.
            from src.api.main import _scheduled_runs, _scheduled_runs_lock
            with _scheduled_runs_lock:
                r['active'] = any(getattr(run,'collection_id',None)==r['id'] for run in _scheduled_runs.values())
            if not r['active']:
                r['status']='interrupted'
        r['saved_posts'] = sum(p.get('type','post')!='comment' for p in store.rows(r['id']))
    return {'runs':runs,'offset':offset,'has_more':len(runs)==30}


@router.post('/api/collections/{rid}/resume')
async def resume(rid: str, comments_only: bool=False):
    store = Store()
    try:
        record = store.get(rid)
    except ValueError as exc:
        raise HTTPException(404,str(exc))
    if record['owner']:
        # Scheduled recovery must use the scheduler's claim and export/analysis lifecycle.
        from src.api.main import run_scheduled_task_now
        return await run_scheduled_task_now(record['owner'], collection_id=rid, comments_only=comments_only)
    # Claim before resetting checkpoints; competing resumes cannot mutate an active run.
    return await start(None, rid=rid, comments_only=comments_only, reset_search=not comments_only)


@router.get('/api/collections/{rid}/result')
async def saved_result(rid: str):
    try:
        return await run_in_threadpool(result, Store(), rid)
    except ValueError as exc:
        raise HTTPException(404,str(exc))

@router.post('/api/schedule-preview')
async def preview(body: dict):
    from src.collection.schedule import describe
    try:
        return describe(body.get('expression',''),body.get('timezone','Asia/Shanghai'),body.get('dialect','unix'))
    except (ValueError, KeyError) as exc:
        raise HTTPException(400,str(exc))

@router.post('/api/collection-preflight')
async def preflight(body: dict):
    try:
        spec=CollectionSpec(**{**body,'probe':True,'keywords':body.get('keywords') or ['test']})
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    return await start(spec,body.get('cookies',[]))
