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
