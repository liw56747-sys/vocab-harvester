"""Image traversal, overlapping downloads and fair cross-platform execution."""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import httpx
import pytest

from src.collection import engine
from src.collection.engine import CollectionSpec, Traffic, collect
from src.collection.images import Images
from src.collection.store import Store
from src.crawlers.progress import report_rows
from src.crawlers.reddit_crawler import RedditCookieFetcher
from src.crawlers.twitter_url import TwitterCookieFetcher
from tests.test_comment_collection import ReplyPage, comment, listing, no_wait
from tests.test_image_collection import png

COOKIES = {'twitter': {'ct0': 'fake', 'auth_token': 'fake'},
           'reddit': {'reddit_session': 'fake'}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('VOCAB_DATA_DIR', str(tmp_path))
    Traffic.active.clear(); Traffic.until.clear(); Traffic.delay.clear()
    yield
    assert not Traffic.active and not Traffic.waiters
    Traffic.until.clear(); Traffic.delay.clear()


class ImagePage(ReplyPage):
    async def evaluate(self, script, arg=None):
        if script == TwitterCookieFetcher._EXTRACT_REPLIES_JS:
            assert arg['imagesOnly']
            self.rounds += 1
            batch = self.batches.pop(0) if self.batches else []
            return [{'tweetId': str(i), 'imageUrls': [f'https://pbs.twimg.com/media/{i}.png'] if i % 6 == 0 else []}
                    for i in batch if str(i) not in arg['excludeIds']]
        # Image traversal must never expand long comment bodies.
        assert 'expand' not in script.lower()
        return await super().evaluate(script, arg)


async def test_x_crosses_image_free_screens_and_ignores_advertised_comment_total(no_wait):
    fetcher = TwitterCookieFetcher(); fetcher._image_collection = True
    page = ImagePage([[1, 2], [3, 4], [5, 6], [7, 8]])
    progress = {'replies': 9999}
    rows = await fetcher._scrape_replies_page(page, page.url, progress=progress)
    assert [r['tweet_id'] for r in rows] == ['6']
    assert progress['image_comments_scanned'] == 8
    assert progress['comments_status'] == 'finished' and progress['image_scan_exhausted']


async def test_image_scan_limit_is_partial_even_if_no_images(no_wait):
    fetcher = TwitterCookieFetcher(); fetcher._image_collection = True
    page = ImagePage([[1, 2, 3, 4, 5, 6]])
    progress = {}
    rows = await fetcher._scrape_replies_page(page, page.url, max_replies=3, progress=progress)
    assert rows == [] and progress['image_comments_scanned'] == 3
    assert progress['comments_status'] == 'partial' and not progress['image_scan_exhausted']
    assert progress['comments_reason'] == 'limit'


async def test_reddit_follows_image_free_parent_and_morechildren(no_wait, monkeypatch):
    fetcher = RedditCookieFetcher(); fetcher._image_collection = True
    nested = comment('child', body='![img](https://i.redd.it/nested.png)')
    parent = comment('parent', replies={'data': {'children': [nested]}})
    more = {'kind': 'more', 'data': {'children': ['later']}}
    later = comment('later', body='text ![img](https://i.redd.it/later.png)')
    request = AsyncMock(side_effect=[[{}, listing([parent, more])],
        {'json': {'data': {'things': [later]}}}])
    monkeypatch.setattr(fetcher, '_get_comment_json', request)
    progress = {'num_comments': 9999}
    rows = await fetcher._fetch_post_comments(None, 'test', 'root', progress=progress)
    assert {r['comment_id'] for r in rows} == {'child', 'later'}
    assert all('content' not in r for r in rows)
    assert request.await_count == 2 and progress['image_comments_scanned'] == 3
    assert progress['comments_status'] == 'finished' and progress['image_scan_exhausted']


async def test_discovered_image_downloads_before_comments_finish_and_filters_storage(monkeypatch):
    store = Store(); rid = store.create(CollectionSpec(keywords=['x'], count=1, content_mode='images').model_dump(mode='json'))
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', AsyncMock(return_value=([
        {'tweet_id': '1', 'image_urls': [], 'content': 'PRIVATE'}], '')))
    downloaded = asyncio.Event()
    async def response(request):
        downloaded.set()
        return httpx.Response(200, content=png())
    original = Images.download
    async def download(self, *args, **kwargs):
        return await original(self, *args, **kwargs, transport=httpx.MockTransport(response))
    monkeypatch.setattr(Images, 'download', download)
    async def fetch(fetcher, platform, posts, cookies):
        p = posts[0]
        p['replies_data'] = [{'tweet_id': 'plain', 'content': 'PRIVATE'},
            {'tweet_id': 'picture', 'content': 'PRIVATE', 'image_urls': ['https://i.redd.it/early.png']}]
        report_rows([p])
        await asyncio.wait_for(downloaded.wait(), 2)
        p['comments_status'] = 'finished'
        return posts
    monkeypatch.setattr(engine, 'fetch_comments', fetch)
    rows, info = await collect(store, rid, COOKIES)
    assert info['status'] == 'success'
    assert [r['tweet_id'] for r in rows[0]['replies_data']] == ['picture']
    assert 'PRIVATE' not in json.dumps(store.rows(rid))
    assert Images(store).summary(rid)['images_downloaded'] == 1


async def test_cancellation_joins_streaming_downloader_and_leaves_resumable_asset(monkeypatch):
    store = Store(); rid = store.create(CollectionSpec(keywords=['x'], count=1, content_mode='images').model_dump(mode='json'))
    entered, closed = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', AsyncMock(return_value=([
        {'tweet_id': '1', 'image_urls': ['https://i.redd.it/a.png']}], '')))
    async def one(self, client, *args):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()
    monkeypatch.setattr(Images, '_one', one)
    async def fetch(*args):
        await asyncio.Future()
    monkeypatch.setattr(engine, 'fetch_comments', fetch)
    task = asyncio.create_task(collect(store, rid, COOKIES))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and store.get(rid)['status'] == 'interrupted'
    assert Images(store).summary(rid)['images_pending'] == 1
    assert len(store.rows(rid)) == 1


async def test_two_runs_use_free_platform_while_x_is_busy(monkeypatch):
    store = Store()
    spec = CollectionSpec(keywords=['first'], platforms=['twitter', 'reddit'], count=1)
    first = store.create(spec.model_dump(mode='json'))
    spec.keywords = ['second']
    second = store.create(spec.model_dump(mode='json'))
    x_started, release_x, reddit_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def x_search(self, keyword, **kwargs):
        if keyword == 'first':
            x_started.set()
            await release_x.wait()
        return [{'tweet_id': keyword}], ''
    async def reddit_search(self, keyword, **kwargs):
        if keyword == 'second': reddit_second.set()
        return [{'post_id': keyword}], ''
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', x_search)
    monkeypatch.setattr(RedditCookieFetcher, 'search_posts', reddit_search)
    a = asyncio.create_task(collect(store, first, COOKIES))
    await asyncio.wait_for(x_started.wait(), 2)
    b = asyncio.create_task(collect(store, second, COOKIES))
    try:
        await asyncio.wait_for(reddit_second.wait(), 2)
        assert not release_x.is_set()
        assert store.progress(second)['waiting_platforms'] == ['twitter']
    finally:
        release_x.set()
        results = await asyncio.gather(a, b)
    assert all(info['status'] == 'success' for _, info in results)
    assert {r['keyword'] for r in store.rows(first)} == {'first'}
    assert {r['keyword'] for r in store.rows(second)} == {'second'}


async def test_legacy_shared_total_does_not_double_allocate_platform_quota(monkeypatch):
    store = Store()
    spec = CollectionSpec(keywords=['first', 'second'], platforms=['twitter', 'reddit'],
                          count=3, count_scope='total')
    rid = store.create(spec.model_dump(mode='json'))
    async def search(fetcher, keyword, count, **kwargs):
        await asyncio.sleep(.001)
        return [dict(tweet_id=keyword+str(i), post_id=keyword+str(i)) for i in range(count)], ''
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', search)
    reddit = AsyncMock(side_effect=search)
    monkeypatch.setattr(RedditCookieFetcher, 'search_posts', reddit)
    rows, info = await collect(store, rid, COOKIES)
    assert len(rows) == info['requested_posts'] == 3
    assert info['status'] == 'success' and reddit.await_count == 0


async def test_unexpected_downloader_failure_cancels_platform_workers_promptly(monkeypatch):
    store = Store(); rid = store.create(CollectionSpec(keywords=['x'], count=1, content_mode='images').model_dump(mode='json'))
    entered, closed = asyncio.Event(), asyncio.Event()
    async def search(*args, **kwargs):
        entered.set()
        try: await asyncio.Future()
        finally: closed.set()
    async def download(*args, **kwargs):
        await entered.wait()
        raise RuntimeError('simulated storage failure')
    monkeypatch.setattr(TwitterCookieFetcher, 'search_tweets', search)
    monkeypatch.setattr(Images, 'download', download)
    with pytest.raises(RuntimeError, match='simulated storage failure'):
        await asyncio.wait_for(collect(store, rid, COOKIES), 2)
    assert closed.is_set() and store.get(rid)['status'] == 'failed'


async def test_fair_gate_reacquirer_cannot_overtake_waiting_run():
    await Traffic.enter('twitter', lambda: False)
    order = []
    async def enter(name):
        await Traffic.enter('twitter', lambda: False)
        order.append(name)
        Traffic.leave('twitter')
    waiter = asyncio.create_task(enter('waiting'))
    await asyncio.sleep(.01)
    Traffic.leave('twitter')
    reacquirer = asyncio.create_task(enter('previous-owner'))
    await asyncio.wait_for(asyncio.gather(waiter, reacquirer), 2)
    assert order == ['waiting', 'previous-owner']


async def test_two_resumed_comment_runs_do_not_block_free_platform_or_mix_parents(monkeypatch):
    store = Store(); runs = []
    for keyword in ['first', 'second']:
        spec = CollectionSpec(keywords=[keyword], platforms=['twitter', 'reddit'], count=1, include_replies=True)
        rid = store.create(spec.model_dump(mode='json')); runs.append(rid)
        for platform in spec.platforms:
            uid = hashlib.sha256((platform+'\0'+keyword).encode()).hexdigest()[:24]
            ident = keyword+'-'+platform
            store.save(rid, uid, [dict(platform=platform, keyword=keyword, tweet_id=ident, post_id=ident,
                comments_status='pending')], {'target': 1, 'search_finished': True})
    started, release, other_platform = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def fetch(fetcher, platform, posts, cookies):
        post = posts[0]; ident = post['post_id']
        if ident == 'first-twitter':
            started.set(); await release.wait()
        if ident == 'second-reddit': other_platform.set()
        post['comments_status'] = 'finished'
        if platform == 'twitter':
            post['replies_data'] = [{'tweet_id': ident+'-reply'}]
            return posts
        return [post, dict(type='comment', comment_id=ident+'-reply', post_id=ident, root_post_id=ident)]
    monkeypatch.setattr(engine, 'fetch_comments', fetch)
    a = asyncio.create_task(collect(store, runs[0], COOKIES, comments_only=True))
    await asyncio.wait_for(started.wait(), 2)
    b = asyncio.create_task(collect(store, runs[1], COOKIES, comments_only=True))
    try:
        await asyncio.wait_for(other_platform.wait(), 2)
        assert not release.is_set()
    finally:
        release.set(); results = await asyncio.gather(a, b)
    assert all(info['status'] == 'success' for _, info in results)
    for rid, keyword in zip(runs, ['first', 'second']):
        rows = store.rows(rid)
        assert {r['keyword'] for r in rows} == {keyword}
        assert {r['post_id'] for r in rows} == {keyword+'-twitter', keyword+'-reddit'}
        assert comment_summary_for_test(rows) == 2


def comment_summary_for_test(rows):
    return sum(len(r.get('replies_data', [])) if r.get('type') != 'comment' else 1 for r in rows)


async def test_cancelled_gate_waiter_does_not_block_later_jobs():
    await Traffic.enter('twitter', lambda: False)
    waiter = asyncio.create_task(Traffic.enter('twitter', lambda: False))
    await asyncio.sleep(.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    Traffic.leave('twitter')
    await asyncio.wait_for(Traffic.enter('twitter', lambda: False), 1)
    Traffic.leave('twitter')


async def test_cross_thread_gates_keep_platforms_independent_and_exclusive():
    active, peaks = dict(twitter=0, reddit=0), dict(twitter=0, reddit=0)
    async def worker():
        for _ in range(4):
            for platform in ['twitter', 'reddit']:
                await Traffic.enter(platform, lambda: False)
                try:
                    with Traffic.lock:
                        active[platform] += 1
                        peaks[platform] = max(peaks[platform], active[platform])
                    await asyncio.sleep(.001)
                    with Traffic.lock: active[platform] -= 1
                finally:
                    Traffic.leave(platform)
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=3) as pool:
        await asyncio.gather(*[loop.run_in_executor(pool, asyncio.run, worker()) for _ in range(3)])
    assert peaks == {'twitter': 1, 'reddit': 1} and not any(active.values())
