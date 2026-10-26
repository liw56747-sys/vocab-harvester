"""Bounded comment-session reuse, truthful shortfalls and retained limit protection."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.collection import engine
from src.collection.comment_session import CommentSession, CommentOutcomeError
from src.collection.engine import CollectionSpec, Traffic
from src.collection.store import Store
from src.crawlers.browser_manager import BrowserManager
from src.crawlers.progress import report_rows
from src.crawlers.twitter_url import TwitterCookieFetcher
from src.crawlers.reddit_crawler import RedditCookieFetcher
from tests.test_comment_collection import listing, comment

COOKIES={'twitter':{'ct0':'test','auth_token':'account-one'},'reddit':{'reddit_session':'account-two'}}


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR',str(tmp_path))
    Traffic.active.clear();Traffic.delay.clear();Traffic.until.clear()
    yield
    Traffic.active.clear();Traffic.delay.clear();Traffic.until.clear()


class Page:
    def __init__(self, context):
        self.context=context;self.closed=False;self.url='about:blank';self.visits=[]
        self.route=AsyncMock()
    def is_closed(self):return self.closed
    def set_default_timeout(self,value):pass
    async def goto(self,url,**kwargs):
        self.url=url;self.visits.append(url)
    async def close(self):self.closed=True


class Context:
    def __init__(self,cookies,proxy):
        self.cookies=dict(cookies);self.proxy=proxy;self.closed=False;self.handlers=[];self.parents=[];self.pages=[]
    def on(self,event,fn):
        assert event=='close';self.handlers.append(fn)
    async def new_page(self):
        page=Page(self);self.pages.append(page);return page
    async def close(self):
        self.closed=True
        for page in self.pages:await page.close()
        for fn in self.handlers:fn()


def fake_browser(monkeypatch):
    contexts=[]
    async def new_context(cookies,proxy):
        ctx=Context(cookies,proxy);contexts.append(ctx);return ctx
    monkeypatch.setattr(BrowserManager,'get',lambda:SimpleNamespace(new_context=new_context))
    return contexts


def make_run(platforms=('twitter',),keywords=('a','b'),count=2):
    store=Store();rid=store.create(CollectionSpec(keywords=list(keywords),platforms=list(platforms),count=count,include_replies=True).model_dump(mode='json'))
    return store,rid


async def x_search(self,keyword,count,**kwargs):
    return [dict(tweet_id=f'{keyword}-{i}',post_id=f'{keyword}-{i}',type='post',url=f'https://x.com/i/status/{keyword}-{i}') for i in range(count)],''


async def test_200_x_parents_use_ten_bounded_contexts_and_keep_all_links(monkeypatch):
    contexts=fake_browser(monkeypatch)
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',x_search)
    async def replies(self,context,posts):
        assert not context.closed
        assert sum(not c.closed for c in contexts)==1
        for p in posts:
            context.parents.append(p['tweet_id'])
            p.update(comments_status='finished',comments_reason='',replies_data=[{'tweet_id':p['tweet_id']+'-reply','content':'saved'}])
        report_rows(posts)
    monkeypatch.setattr(TwitterCookieFetcher,'_parallel_scrape_replies',replies)
    store,rid=make_run(count=100)
    rows,info=await engine.collect(store,rid,COOKIES)
    assert len(contexts)==10 and all(c.closed and len(c.parents)==20 for c in contexts)
    assert len(rows)==200 and info['comments_pending']==0 and info['status']=='success'
    assert all(r['replies_data'][0]['tweet_id']==r['tweet_id']+'-reply' for r in rows)
    # Identical calls through the standalone compatibility path retain the old per-parent ownership.
    contexts.clear();fetcher=TwitterCookieFetcher()
    for i in range(200):
        await engine.fetch_comments(fetcher,'twitter',[{'tweet_id':str(i)}],COOKIES['twitter'])
    assert len(contexts)==200 and all(c.closed and len(c.parents)==1 for c in contexts)


async def test_closed_context_recreated_and_accounts_never_share(monkeypatch):
    contexts=fake_browser(monkeypatch)
    one=CommentSession(TwitterCookieFetcher(proxy='http://one.test:80'),'twitter',{'ct0':'one','auth_token':'one'})
    two=CommentSession(TwitterCookieFetcher(proxy='http://two.test:80'),'twitter',{'ct0':'two','auth_token':'two'})
    first=await one.twitter_context();second=await two.twitter_context()
    assert first is not second and first.cookies!=second.cookies and first.proxy!=second.proxy
    await first.close()
    assert await one.twitter_context() is not first
    assert await two.twitter_context() is second
    await one.close();await two.close()
    assert all(c.closed for c in contexts)


async def test_cancel_closes_context_and_resume_uses_updated_cookies(monkeypatch):
    contexts=fake_browser(monkeypatch);blocked=asyncio.Event();calls=[]
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',x_search)
    async def replies(self,context,posts):
        p=posts[0];calls.append(p['tweet_id'])
        p.update(comments_status='partial',comments_reason='network',replies_data=[dict(tweet_id=p['tweet_id']+'-first')])
        report_rows(posts)
        blocked.set()
        await asyncio.Future()
    monkeypatch.setattr(TwitterCookieFetcher,'_parallel_scrape_replies',replies)
    store,rid=make_run()
    task=asyncio.create_task(engine.collect(store,rid,COOKIES))
    await asyncio.wait_for(blocked.wait(),5);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert len(contexts)==1 and contexts[0].closed
    assert next(r for r in store.rows(rid) if r['tweet_id']=='a-0')['replies_data'][0]['tweet_id']=='a-0-first'
    async def recovered(self,context,posts):
        assert context.cookies['auth_token']=='updated'
        posts[0].update(comments_status='finished',comments_reason='',replies_data=[dict(tweet_id=posts[0]['tweet_id']+'-second')])
    monkeypatch.setattr(TwitterCookieFetcher,'_parallel_scrape_replies',recovered)
    rows,info=await engine.collect(store,rid,{'twitter':{'ct0':'test','auth_token':'updated'}})
    assert len(contexts)==2 and contexts[1].closed and info['comments_pending']==0
    assert {r['tweet_id'] for r in next(p for p in rows if p['tweet_id']=='a-0')['replies_data']}=={'a-0-first','a-0-second'}


async def test_reddit_reuses_one_client_across_keywords_with_rotated_cookies(monkeypatch):
    clients=[];seen=[]
    async def search(self,keyword,count,**kwargs):
        self._session_cookies=httpx.Cookies({'reddit_session':'account-two','search_nonce':'retained'})
        return [dict(post_id=f'{keyword}-{i}',subreddit='test',type='post',num_comments=1) for i in range(count)],''
    def handler(request):
        seen.append(request)
        assert 'search_nonce=retained' in request.headers['cookie']
        if len(seen)>1:assert 'reply_nonce=updated' in request.headers['cookie']
        pid=request.url.path.rsplit('/',1)[-1].removesuffix('.json')
        return httpx.Response(200,headers={'set-cookie':'reply_nonce=updated; Domain=.reddit.com; Path=/'},json=[{},listing([comment(pid+'-reply')])])
    real_client=httpx.AsyncClient
    def client(**kwargs):
        c=real_client(transport=httpx.MockTransport(handler),**kwargs);clients.append(c);return c
    monkeypatch.setattr(httpx,'AsyncClient',client)
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',search)
    store,rid=make_run(platforms=('reddit',),count=25)
    rows,info=await engine.collect(store,rid,COOKIES)
    assert len(clients)==1 and clients[0].is_closed
    assert len(seen)==50 and info['comments_finished']==50
    assert len([r for r in rows if r.get('type')=='comment'])==50


@pytest.mark.parametrize('reason',['shortfall','limit','unavailable'])
def test_incomplete_count_never_causes_exponential_platform_backoff(reason):
    for _ in range(200):
        Traffic.leave('twitter',CommentOutcomeError({'comments_reason':reason,'comments_warning':'评论仍有缺口'}),elapsed=1)
        assert Traffic.delay['twitter']==0


@pytest.mark.parametrize('message',['429 rate limit','network timeout','503 service unavailable'])
def test_real_transient_failures_still_back_off(message):
    delays=[]
    for _ in range(10):
        Traffic.leave('twitter',RuntimeError(message),elapsed=1)
        delays.append(Traffic.delay['twitter'])
    assert delays==sorted(delays) and delays[-1]==120
    assert delays[0]==(15 if '429' in message else 2)


async def test_shortfalls_keep_partial_results_without_accumulating_delay(monkeypatch):
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',x_search)
    async def comments(fetcher,platform,posts,cookies):
        posts[0].update(comments_status='partial',comments_reason='shortfall',comments_warning='页面显示更多评论，部分不可访问')
        return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    store,rid=make_run(count=20)
    _,info=await asyncio.wait_for(engine.collect(store,rid,COOKIES),10)
    assert info['status']=='partial' and info['comments_pending']==40
    assert Traffic.delay['twitter']==0


@pytest.mark.parametrize('reason,attempts',[('authentication',1),('rate_limit',1),('access_denied',3)])
async def test_platform_comment_failures_pause_rest_but_keep_other_platform_and_resume(monkeypatch,reason,attempts):
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',x_search)
    async def reddit(self,keyword,count,**kwargs):return [dict(post_id=f'{keyword}-{i}',type='post') for i in range(count)],''
    monkeypatch.setattr(RedditCookieFetcher,'search_posts',reddit)
    calls=[]
    async def comments(fetcher,platform,posts,cookies):
        p=posts[0];calls.append((platform,p.get('post_id')))
        if platform=='twitter':p.update(comments_status='failed',comments_reason=reason,comments_warning=reason)
        else:p.update(comments_status='finished',comments_reason='')
        return posts
    monkeypatch.setattr(engine,'fetch_comments',comments)
    # Keep the policy logic; do not spend wall time on the test's deliberate failures.
    real_leave=Traffic.leave
    def leave(*a,**kw):real_leave(*a,**kw);Traffic.until.clear()
    monkeypatch.setattr(Traffic,'leave',leave)
    store,rid=make_run(platforms=('twitter','reddit'),count=5)
    rows,info=await engine.collect(store,rid,COOKIES)
    assert sum(p=='twitter' for p,_ in calls)==attempts
    assert sum(p=='reddit' for p,_ in calls)==10
    assert len(rows)==20 and info['comments_pending']==10 and info['status']=='partial'
    assert all(r['comments_reason']==reason for r in rows if r['platform']=='twitter')
    calls.clear()
    async def recovered(fetcher,platform,posts,cookies):
        calls.append(platform);posts[0].update(comments_status='finished',comments_reason='');return posts
    monkeypatch.setattr(engine,'fetch_comments',recovered)
    _,info=await engine.collect(store,rid,COOKIES,comments_only=True)
    assert calls==['twitter']*10 and info['comments_pending']==0


@pytest.mark.parametrize('warning,expected',[
    ('503 Service Unavailable', 'platform'),
    ('HTTP status 502', 'platform'),
    ('Server error \'500 Internal Server Error\'', 'platform'),
    ('页面显示 500 条评论，实际采集 1 条', 'shortfall'),
    ('达到单帖评论上限 5000 条', 'limit'),
])
def test_server_error_classification_does_not_confuse_comment_counts(warning,expected):
    from src.crawlers.comment_state import error_reason
    assert error_reason(warning)[0]==expected


async def test_reused_page_is_blank_between_parents_and_network_failure_discards_it(monkeypatch):
    contexts=fake_browser(monkeypatch);attempts=[]
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',x_search)
    monkeypatch.setattr(asyncio,'sleep',AsyncMock())
    async def scrape(self,page,url,*,progress):
        assert page.url=='about:blank'
        await page.goto(url)
        identity=progress['tweet_id'];attempts.append(identity)
        if identity=='a-1' and attempts.count(identity)==1:
            raise RuntimeError('network timeout')
        progress.update(comments_status='finished',comments_reason='',replies_count=1,
                        replies_data=[{'tweet_id':identity+'-reply'}])
    monkeypatch.setattr(TwitterCookieFetcher,'_scrape_replies_page',scrape)
    store,rid=make_run(count=3)
    spec=store.get(rid)['spec'];spec['block_resources']=True;store.update_spec(rid,spec)
    rows,info=await engine.collect(store,rid,COOKIES)
    assert info['comments_finished']==6 and info['comments_pending']==0
    assert len(contexts)==1 and len(contexts[0].pages)==2
    assert all(page.closed and page.route.await_count==1 for page in contexts[0].pages)
    assert attempts.count('a-1')==2 and len(attempts)==7
    assert sum(page.visits.count('about:blank') for page in contexts[0].pages)==6
    assert all(r['replies_data'][0]['tweet_id']==r['tweet_id']+'-reply' for r in rows)


async def test_cancellation_with_real_comment_loop_closes_leased_page_and_saves_partial(monkeypatch):
    contexts=fake_browser(monkeypatch);blocked=asyncio.Event()
    monkeypatch.setattr(TwitterCookieFetcher,'search_tweets',x_search)
    async def scrape(self,page,url,*,progress):
        await page.goto(url)
        progress.update(comments_status='partial',comments_reason='network',replies_count=1,
                        replies_data=[{'tweet_id':progress['tweet_id']+'-saved'}])
        report_rows([progress]);blocked.set()
        await asyncio.Future()
    monkeypatch.setattr(TwitterCookieFetcher,'_scrape_replies_page',scrape)
    store,rid=make_run()
    task=asyncio.create_task(engine.collect(store,rid,COOKIES))
    await asyncio.wait_for(blocked.wait(),5);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert all(c.closed and all(p.closed for p in c.pages) for c in contexts)
    assert len(contexts[0].pages)==2
    saved=next(r for r in store.rows(rid) if r['tweet_id']=='a-0')
    assert saved['replies_data'][0]['tweet_id']=='a-0-saved'
    assert not Traffic.active
