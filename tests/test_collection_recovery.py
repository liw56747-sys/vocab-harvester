"""Behavioral tests at the durable engine boundary, with simulated platform responses."""
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from src.collection import engine
from src.collection.engine import CollectionSpec, Traffic, collect, classify, partitions
from src.collection.store import Store, merge
from src.crawlers.progress import report_rows
from src.crawlers.twitter_url import TwitterCookieFetcher
from src.crawlers.reddit_crawler import RedditCookieFetcher

COOKIES = {'twitter':{'ct0':'test','auth_token':'test'},'reddit':{'reddit_session':'test'}}


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR',str(tmp_path))
    Traffic.active.clear(); Traffic.delay.clear(); Traffic.until.clear()
    yield
    Traffic.active.clear(); Traffic.delay.clear(); Traffic.until.clear()


def tweet(i, **extra):
    return {'tweet_id':str(i),'content':f'post {i}','url':f'https://x.com/a/status/{i}',
            'created_at':'2026-10-14T10:00:00+00:00', **extra}


def create(**kwargs):
    store=Store()
    rid=store.create(CollectionSpec(keywords=['one'],**kwargs).model_dump(mode='json'))
    return store,rid


async def test_process_exit_keeps_checkpoint_and_resume_does_not_duplicate(monkeypatch,tmp_path):
    path=tmp_path/'collection.db'
    # Commit in a different real process, then terminate without normal shutdown.
    code="""import os,sys
from src.collection.store import Store
from src.collection.engine import CollectionSpec
s=Store(sys.argv[1]); r=s.create(CollectionSpec(keywords=['one'],count=3).model_dump(mode='json'),run_id='crashed')
import hashlib
unit=hashlib.sha256(b'twitter\\0one').hexdigest()[:24]
s.save(r,unit,[{'platform':'twitter','keyword':'one','tweet_id':'1','content':'saved','type':'post'}],{'partition':0,'requested':3})
s.status(r,'running')
os._exit(23)
"""
    proc=subprocess.run([sys.executable,'-c',code,str(path)])
    assert proc.returncode==23
    async def search(self,kw,**kwargs):
        assert kwargs['count']==3
        return [tweet(1),tweet(2),tweet(3)],''
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    rows,info=await collect(Store(path),'crashed',COOKIES)
    assert [r['tweet_id'] for r in rows]==['1','2','3']
    assert info['status']=='success'


async def test_cancel_retains_comments_and_recovery_only_visits_pending_parents(monkeypatch):
    store,rid=create(count=2,include_replies=True)
    search=AsyncMock(return_value=([tweet(1),tweet(2)],''))
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    calls=[]
    async def comments(fetcher,platform,posts,cookies):
        p=posts[0]; calls.append(p['tweet_id'])
        p['replies_data']=[{'tweet_id':'reply'+p['tweet_id'],'content':'comment'}]
        p['comments_status']='finished' if p['tweet_id']=='1' else 'partial'
        report_rows(posts)
        if p['tweet_id']=='2':
            raise asyncio.CancelledError
        return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    with pytest.raises(asyncio.CancelledError):
        await collect(store,rid,COOKIES)
    assert len(store.rows(rid))==2
    async def finish(fetcher,platform,posts,cookies):
        assert posts[0]['tweet_id']=='2'
        posts[0].update(comments_status='finished',replies_data=[{'tweet_id':'reply2','content':'comment'}, {'tweet_id':'new','content':'new'}])
        return posts
    monkeypatch.setattr(engine,'fetch_comments',finish)
    rows,info=await collect(Store(store.path),rid,COOKIES,comments_only=True)
    assert search.await_count==1
    assert len(rows[1]['replies_data'])==2
    assert info['comments_pending']==0


@pytest.mark.parametrize('scope,expected',[('per_platform',3),('per_keyword',6),('total',3)])
async def test_count_scopes_deduplicate_overlapping_keywords(monkeypatch,scope,expected):
    spec=CollectionSpec(keywords=['one','two'],count=3,count_scope=scope)
    store=Store();rid=store.create(spec.model_dump(mode='json'))
    async def search(self,keyword,count,**kwargs):
        source=[tweet(1),tweet(2)] if keyword=='one' else [tweet(i) for i in range(1,9)]
        return source[:count],''
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    rows,info=await collect(store,rid,COOKIES)
    assert len({r['tweet_id'] for r in rows})==len(rows)
    assert info['requested_posts']==expected
    assert len(rows)==(5 if scope=='per_keyword' else 3)
    assert info['missing_posts']==(1 if scope=='per_keyword' else 0)


async def test_auth_failure_not_retried_and_new_credentials_used_for_resume(monkeypatch):
    store,rid=create(count=1,max_retries=3)
    search=AsyncMock(side_effect=RuntimeError('Cookie 已过期'))
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    _,info=await collect(store,rid,COOKIES)
    assert search.await_count==1 and info['status']=='partial'
    Traffic.until.clear()
    search.side_effect=None;search.return_value=([tweet(1)],'')
    await collect(store,rid,{'twitter':{'ct0':'new','auth_token':'new'}})
    assert search.call_args.kwargs['cookies']['auth_token']=='new'
    assert 'new' not in store.get(rid)['spec']
    assert 'auth_token' not in json.dumps(store.get(rid)['spec'])


async def test_network_failure_retries_but_keeps_snapshot(monkeypatch):
    store,rid=create(count=2)
    calls=[]
    async def search(self,*args,**kwargs):
        calls.append(1)
        report_rows([tweet(1)])
        if len(calls)==1:
            raise RuntimeError('network timeout')
        return [tweet(1),tweet(2)],''
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    # Remove wall clock waiting, retain the real retry classification/control flow.
    real_leave=Traffic.leave
    def leave(*args,**kwargs):
        real_leave(*args,**kwargs);Traffic.until.clear()
    monkeypatch.setattr(Traffic,'leave',leave)
    rows,info=await collect(store,rid,COOKIES)
    assert len(calls)==2 and len(rows)==2 and info['status']=='success'


def test_date_windows_language_variants_and_reddit_does_not_fake_language_support():
    spec=CollectionSpec(keywords=['a'],since='2026-10-01',until='2026-10-05',window_days=2,languages=['zh','en'],alternate_sort=True,keyword_groups=[['b','c']])
    x=list(partitions(spec,'twitter','a'))
    assert len(x)==16
    assert x[0][0]=='a since:2026-10-03 until:2026-10-05 lang:zh'
    assert len(list(partitions(spec,'reddit','a')))==8


async def test_date_filter_rejects_unknown_outside_dates(monkeypatch):
    store,rid=create(count=2,since='2026-10-14',until='2026-10-15')
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',AsyncMock(return_value=([
        tweet(1),tweet(2,created_at='2026-10-15T00:00:00Z'),tweet(3,created_at='unknown')],'')))
    rows,info=await collect(store,rid,COOKIES)
    assert [r['tweet_id'] for r in rows]==['1']
    assert info['status']=='partial'


async def test_incremental_new_posts_and_old_comment_revisit_are_separate(monkeypatch):
    store=Store()
    spec=CollectionSpec(keywords=['one'],count=1,incremental=True,include_replies=True)
    old=store.create(spec.model_dump(mode='json'),owner='schedule')
    store.save(old,'unit',[{**tweet(1,created_at=datetime.now(timezone.utc).isoformat()),'platform':'twitter','keyword':'one','comments_status':'finished'}],{})
    store.status(old,'success')
    rid=store.create(spec.model_dump(mode='json'),owner='schedule')
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',AsyncMock(return_value=([tweet(1),tweet(2)],'')))
    async def comments(fetcher,platform,posts,cookies):
        posts[0].update(comments_status='finished',replies_data=[{'tweet_id':'r'+posts[0]['tweet_id'],'content':'new reply'}])
        return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    rows,info=await collect(store,rid,COOKIES)
    assert {r['tweet_id'] for r in rows}=={'1','2'}
    assert next(r for r in rows if r['tweet_id']=='1')['collection_kind']=='revisit'
    assert info['missing_posts']==0 and info['status']=='success'
    assert store.watermark('schedule')==store.get(rid)['created']


async def test_global_gate_bounds_two_platforms_and_cancellation_does_not_leak_slot():
    await Traffic.enter('twitter',lambda:False)
    waiting=asyncio.create_task(Traffic.enter('twitter',lambda:False))
    await asyncio.sleep(.01)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await Traffic.enter('reddit',lambda:False)
    assert Traffic.active=={'twitter','reddit'}
    Traffic.leave('twitter',RuntimeError('429'))
    assert Traffic.delay['twitter']>=15
    assert Traffic.until['twitter']>0
    Traffic.leave('reddit')


def test_acceleration_keeps_image_metadata_and_comment_identity():
    rows=[tweet(1,media_urls='https://example/image.jpg',has_media=True)]
    TwitterCookieFetcher(block_resources=True)._strip_media_urls(rows)
    assert rows[0]['has_media'] and rows[0]['media_urls']
    a={'platform':'reddit','type':'comment','post_id':'a','content':'same'}
    b={**a,'post_id':'b'}
    assert len(merge([a],[b]))==2


@pytest.mark.parametrize('message,kind,retry',[('403','access_denied',False),('401','authentication',False),('429','rate_limit',True),('proxy timeout','network',True),('503','platform',True),('bad parse','unknown',False)])
def test_failure_classes(message,kind,retry):
    assert classify(message)[:2]==(kind,retry)


async def test_resume_after_last_post_saved_before_search_return(monkeypatch):
    store,rid=create(count=1)
    async def interrupted(*args,**kwargs):
        report_rows([tweet(1)])
        raise asyncio.CancelledError
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',interrupted)
    with pytest.raises(asyncio.CancelledError):
        await collect(store,rid,COOKIES)
    forbidden=AsyncMock(side_effect=AssertionError('should use persisted target'))
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',forbidden)
    rows,info=await collect(store,rid,COOKIES)
    assert len(rows)==1 and info['status']=='success'
    forbidden.assert_not_called()


async def test_saved_target_is_not_lost_when_fetcher_swallows_cancellation(monkeypatch):
    store,rid=create(count=2)
    started=asyncio.Event()
    async def fetch(*args,**kwargs):
        report_rows([tweet(1)])
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return [tweet(1)],''
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',fetch)
    task=asyncio.create_task(collect(store,rid,COOKIES))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(store.rows(rid))==1
    assert store.get(rid)['status']=='interrupted'


def test_resuming_an_older_run_cannot_move_incremental_watermark_backwards():
    store=Store()
    store.watermark('owner','2026-10-15T00:00:00+00:00')
    store.watermark('owner','2026-10-12T00:00:00+00:00')
    assert store.watermark('owner')=='2026-10-15T00:00:00+00:00'


async def test_explicit_legacy_comment_cap_is_honored_without_implicit_reduction(monkeypatch):
    store,rid=create(count=100,include_replies=True,twitter_count_cap_with_replies=2)
    fetch=AsyncMock(return_value=([tweet(1),tweet(2)],''))
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',fetch)
    async def comments(fetcher,platform,posts,cookies):
        posts[0]['comments_status']='finished'
        return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    rows,info=await collect(store,rid,COOKIES)
    assert fetch.call_args.kwargs['count']==2
    assert info['requested_posts']==2 and info['missing_posts']==0
    assert engine.platform_target(CollectionSpec(keywords=['a'],count=100,include_replies=True),'twitter')==100


def test_date_filter_converts_timezone_to_utc():
    from datetime import date
    assert engine.in_window(tweet(1,created_at='2026-10-15T01:00:00+08:00'),date(2026,10,14),date(2026,10,15))


async def test_default_six_keywords_two_platforms_fifty_each(monkeypatch):
    from collections import Counter
    from src.api.main import CollectionOptions
    assert CollectionOptions().count_scope == 'per_keyword'
    spec = CollectionSpec(keywords=['a','b','c','d','e','f'], platforms=['twitter','reddit'], count=50)
    calls = []
    async def search_x(self, keyword, count, **kwargs):
        calls.append(('twitter', keyword, count))
        return [tweet(f'{keyword}-{i}') for i in range(count)], ''
    async def search_reddit(self, keyword, count, **kwargs):
        calls.append(('reddit', keyword, count))
        return [{'post_id':f'{keyword}-{i}', 'content':'post', 'type':'post'} for i in range(count)], ''
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', search_x)
    monkeypatch.setattr(RedditCookieFetcher, 'search_posts', search_reddit)
    store = Store(); rid = store.create(spec.model_dump(mode='json'))
    rows, info = await collect(store, rid, COOKIES)
    assert info['requested_posts'] == 600 and info['missing_posts'] == 0
    assert info['status'] == 'success'
    assert len(calls) == 12 and all(count == 50 for _, _, count in calls)
    assert Counter((r['platform'],r['keyword']) for r in rows) == {
        (p,k):50 for p in spec.platforms for k in spec.keywords}
    assert Counter(r['keyword'] for r in rows) == dict.fromkeys(spec.keywords,100)
    # Reopening the saved run must retain all twelve targets and avoid duplicate searches.
    await collect(Store(store.path), rid, COOKIES)
    assert len(calls) == 12 and len(store.rows(rid)) == 600


@pytest.mark.parametrize('mode',['text','images'])
async def test_all_400_posts_saved_before_first_comment_and_image_download(monkeypatch, mode):
    from src.collection.images import Images
    spec=CollectionSpec(keywords=['a','b','c','d'],platforms=['twitter','reddit'],count=50,
                        include_replies=True,content_mode=mode)
    store=Store();rid=store.create(spec.model_dump(mode='json'))
    events=[]
    monkeypatch.setattr(Traffic,'enter',AsyncMock())
    monkeypatch.setattr(Traffic,'leave',lambda *a,**k:None)
    async def search(self,keyword,**kwargs):
        platform='twitter' if isinstance(self,TwitterCookieFetcher) else 'reddit'
        events.append(('search',platform,keyword))
        return [dict(tweet_id=f'{platform}-{keyword}-{i}',post_id=f'{platform}-{keyword}-{i}',content='parent') for i in range(50)],''
    async def comments(fetcher,platform,posts,cookies):
        assert len(store.rows(rid))==400
        state=store.progress(rid)
        assert state['phase']=='comments' and state['search_units_completed']==8
        assert state['current_platform']==platform and state['current_keyword']==posts[0]['keyword']
        assert state['last_progress_at']
        events.append(('comment',platform,posts[0]['post_id']))
        posts[0]['comments_status']='finished'
        return posts
    async def download(self,*a,**k):
        assert len([e for e in events if e[0]=='comment'])==400
        events.append(('download',))
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',search)
    monkeypatch.setattr(engine,'fetch_comments',comments)
    monkeypatch.setattr(Images,'download',download)
    updates=[];finished=[]
    def platform_event(event,platform):
        if event=='done':
            assert len([e for e in events if e[0]=='comment' and e[1]==platform])==200
            finished.append(platform)
    rows,info=await collect(store,rid,COOKIES,progress=updates.append,platform_event=platform_event)
    assert finished==['twitter','reddit']
    comment_updates=[u for u in updates if u['phase']=='comments']
    assert comment_updates and all(u['saved_posts']==400 for u in comment_updates)
    assert all(u['requested_posts']==400 and u['completed']==8 for u in comment_updates)
    assert [e[0] for e in events[:8]]==['search']*8
    assert len(rows)==400 and info['requested_posts']==400 and info['missing_posts']==0
    assert len(info['count_results'])==8 and info['status']=='success'
    assert store.progress(rid)['phase']=='finished'
    assert (events[-1][0]=='download')==(mode=='images')


async def test_comment_stall_cancel_preserves_all_platforms_and_resume_skips_search(monkeypatch):
    spec=CollectionSpec(keywords=['a','b'],platforms=['twitter','reddit'],count=2,include_replies=True)
    store=Store();rid=store.create(spec.model_dump(mode='json'))
    monkeypatch.setattr(Traffic,'enter',AsyncMock())
    monkeypatch.setattr(Traffic,'leave',lambda *a,**k:None)
    searches=[]
    async def search(self,kw,**kwargs):
        platform='twitter' if isinstance(self,TwitterCookieFetcher) else 'reddit'
        searches.append((platform,kw))
        return [dict(tweet_id=f'{platform}-{kw}-{i}',post_id=f'{platform}-{kw}-{i}') for i in range(2)],''
    entered=asyncio.Event()
    async def stall(*a):
        entered.set();await asyncio.Future()
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',search)
    monkeypatch.setattr(engine,'fetch_comments',stall)
    task=asyncio.create_task(collect(store,rid,COOKIES))
    await asyncio.wait_for(entered.wait(),5)
    assert len(store.rows(rid))==8 and len(searches)==4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    async def finish(fetcher,platform,posts,cookies):
        posts[0]['comments_status']='finished';return posts
    monkeypatch.setattr(engine,'fetch_comments',finish)
    _,info=await collect(Store(store.path),rid,COOKIES)
    assert info['status']=='success' and len(searches)==4


async def test_staged_comments_keep_parent_keyword_and_platform_after_resume(monkeypatch):
    from src.crawlers.comment_results import search_export_rows
    spec=CollectionSpec(keywords=['alpha','beta'],platforms=['twitter','reddit'],count=2,include_replies=True)
    store=Store();rid=store.create(spec.model_dump(mode='json'))
    searches=[];comment_calls=[]
    monkeypatch.setattr(Traffic,'enter',AsyncMock())
    monkeypatch.setattr(Traffic,'leave',lambda *a,**kw:None)
    async def search(self,keyword,**kwargs):
        searches.append(keyword)
        # Same IDs on different platforms must remain distinct.
        return [dict(tweet_id=f'{keyword}-{i}',post_id=f'{keyword}-{i}',type='post',content='parent') for i in range(2)],''
    async def comments(fetcher,platform,posts,cookies):
        p=posts[0];identity=p['post_id'];comment_calls.append((platform,identity))
        p['comments_status']='finished'
        if platform=='twitter':
            p['replies_data']=[dict(tweet_id=identity+'-reply',content=identity)]
            result=posts
        else:
            result=posts+[dict(post_id=identity,comment_id=identity+'-reply',parent_id=identity,type='comment',content=identity)]
        report_rows(result)
        if len(comment_calls)==5:
            raise asyncio.CancelledError
        return result
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',search)
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',search)
    monkeypatch.setattr(engine,'fetch_comments',comments)
    with pytest.raises(asyncio.CancelledError):
        await collect(store,rid,COOKIES)
    assert len(searches)==4
    rows,info=await collect(Store(store.path),rid,COOKIES)
    assert len(searches)==4 and len(comment_calls)==8
    exported=search_export_rows(rows)
    parents={(r['platform'],r['post_id']):r['keyword'] for r in exported if r['type']=='post'}
    replies=[r for r in exported if r['type']=='comment']
    assert len(parents)==8 and len(replies)==8
    assert info['comments_pending']==0
    for reply in replies:
        assert parents[(reply['platform'],reply['parent_id'])]==reply['keyword']
        assert reply['content']==reply['parent_id']
        assert reply['comment_id']==reply['parent_id']+'-reply'


async def test_search_denial_is_visible_before_task_ends_and_resume_clears_it(monkeypatch):
    from src.collection.api import summary
    monkeypatch.setattr(Traffic, 'enter', AsyncMock())
    store = Store()
    spec = CollectionSpec(keywords=['first','second'], platforms=['reddit','twitter'], count=1)
    rid = store.create(spec.model_dump(mode='json'))
    denied = AsyncMock(side_effect=RuntimeError('403 Blocked'))
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',denied)
    live=[]
    async def twitter(self, keyword, **kwargs):
        # Reddit failure must be visible while another platform is still running.
        assert store.get(rid)['status']=='running' and store.get(rid)['error']==''
        current=summary(store,rid)
        assert 'access_denied' in current['error'] and current['errors']
        live.append(current)
        return [tweet(keyword)], ''
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',twitter)
    rows,info=await collect(store,rid,COOKIES)
    assert denied.await_count==1 and len(live)==2
    assert len(rows)==2 and info['status']=='partial'
    reddit_units=[state for _,state in store.units(rid).values() if state['platform']=='reddit']
    assert len(reddit_units)==2 and all(state.get('error') for state in reddit_units)
    async def recovered(self, keyword, **kwargs):
        return [{'post_id':keyword,'type':'post','content':'ok'}],''
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',recovered)
    rows,info=await collect(store,rid,COOKIES)
    assert len(rows)==4 and info['status']=='success'
    assert not summary(store,rid)['errors'] and not summary(store,rid)['error']
