"""Real failure shapes from the 100-post export; all accounts/responses are isolated fakes."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.crawlers.reddit_crawler import RedditCookieFetcher
from src.crawlers.twitter_url import TwitterCookieFetcher
from src.crawlers.comment_state import comment_summary
from src.collection.store import Store
from src.collection.engine import CollectionSpec
from src.collection import api as collection_api
from tests.test_comment_collection import ReplyPage, no_wait, comment, listing, more


async def test_reddit_search_session_and_working_host_used_for_comments(monkeypatch, no_wait):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == 'www.reddit.com':
            return httpx.Response(403)
        if request.url.path == '/search.json':
            return httpx.Response(200, headers={'Set-Cookie': 'session_aux=rotated; Domain=.reddit.com; Path=/'}, json=listing([
                {'kind': 't3', 'data': {'id': 'root', 'subreddit': 'test', 'num_comments': 1, 'permalink': '/r/test/comments/root/'}}
            ]))
        assert 'session_aux=rotated' in request.headers['cookie']
        assert 'Chrome/' in request.headers['user-agent']
        return httpx.Response(200, json=[{}, listing([comment('found')])])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    fetcher = RedditCookieFetcher()
    posts, _ = await fetcher.search_posts('test', count=1, cookies={'reddit_session': 'test'}, include_replies=False)
    assert posts[0]['reddit_endpoint'] == 'old.reddit.com'
    result = await fetcher._fetch_all_comments_parallel(posts, {'reddit_session': 'test'}, {}, None)
    assert len(result) == 2 and posts[0]['comments_status'] == 'finished'
    assert [r.url.host for r in requests] == ['www.reddit.com', 'old.reddit.com', 'old.reddit.com']


async def test_reddit_comment_fallback_covers_morechildren_and_deep_threads(no_wait):
    requests = []
    def handler(request):
        requests.append((request.url.host, request.url.path))
        if request.url.host == 'www.reddit.com':
            return httpx.Response(403)
        if request.url.path == '/api/morechildren.json':
            return httpx.Response(200, json={'json': {'data': {'things': [comment('child')]}}})
        return httpx.Response(200, json=[{}, listing([comment('rootreply'), more(['child'])])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        progress = {'num_comments': 2}
        rows = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root', progress=progress)
    assert len(rows) == 2 and progress['comments_status'] == 'finished'
    assert requests == [('www.reddit.com', '/r/test/comments/root.json'),
                        ('old.reddit.com', '/r/test/comments/root.json'), ('old.reddit.com', '/api/morechildren.json')]


async def test_reddit_zero_is_verified_and_empty_exception_is_classified(monkeypatch, no_wait):
    fetcher = RedditCookieFetcher()
    async def get(client, url, params):
        if 'bad' in url:
            raise httpx.ReadTimeout('')
        return [{}, listing([])]
    monkeypatch.setattr(fetcher, '_get_comment_json', get)
    posts = [{'post_id': 'zero', 'subreddit': 'test', 'num_comments': 0},
             {'post_id': 'bad', 'subreddit': 'test', 'num_comments': 2}]
    await fetcher._fetch_all_comments_parallel(posts, {}, {}, None)
    assert posts[0]['comments_empty_verified'] and posts[0]['comments_status'] == 'finished'
    assert posts[1]['comments_reason'] == 'network'
    assert 'ReadTimeout' in posts[1]['comments_warning']


async def test_reddit_persistent_403_stops_request_storm(no_wait):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(403)
    fetcher = RedditCookieFetcher()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for i in range(100):
            with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
                await fetcher._fetch_post_comments(client, 'test', str(i))
    assert len(seen) == 6  # Three rejected parents, two public endpoints each.
    assert fetcher.comment_circuit_open


async def test_reddit_count_shortfall_cannot_be_finished(no_wait):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{}, listing([comment('one')])])) ) as client:
        progress = {'num_comments': 8}
        await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root', progress=progress)
    assert progress['comments_status'] == 'partial'
    assert progress['comments_reason'] == 'shortfall'


async def test_x_retry_merges_partial_comments_and_closes_all_pages(no_wait):
    first, second = ReplyPage([[1, 2]], fail_at=2), ReplyPage([[2, 3]])
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[first, second]))
    post = {'url': first.url, 'replies': 3, 'replies_data': json.dumps([{'tweet_id': 'old', 'content': 'retained'}])}
    await TwitterCookieFetcher()._parallel_scrape_replies(context, [post])
    assert {r['tweet_id'] for r in json.loads(post['replies_data'])} == {'old', '1', '2', '3'}
    assert post['comments_status'] == 'finished' and not post['comments_warning']
    assert post['comments_attempts'] == 2
    first.close.assert_awaited_once(); second.close.assert_awaited_once()
    second.wait_for_selector.assert_awaited_once_with('article[data-testid="tweet"]', timeout=45000)


async def test_x_partial_no_progress_is_bounded_and_not_claimed_complete(no_wait):
    pages = [ReplyPage([[1]]), ReplyPage([[1]])]
    context = SimpleNamespace(new_page=AsyncMock(side_effect=pages))
    post = {'url': pages[0].url, 'replies': 10}
    await TwitterCookieFetcher()._parallel_scrape_replies(context, [post])
    assert context.new_page.await_count == 2
    assert post['replies_count'] == 1 and post['comments_status'] == 'partial'
    assert post['comments_reason'] == 'shortfall'


async def test_x_deleted_post_is_unavailable_without_retry_loop(no_wait):
    page = ReplyPage([])
    page.goto.return_value = SimpleNamespace(status=404)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    post = {'url': page.url, 'replies_data': [{'tweet_id': 'saved'}]}
    await TwitterCookieFetcher()._parallel_scrape_replies(context, [post])
    assert post['replies_count'] == 1 and post['comments_reason'] == 'unavailable'
    assert post['comments_status'] == 'partial'
    page.close.assert_awaited_once()


def test_summary_reconciles_snapshot_and_never_builds_exports(tmp_path, monkeypatch):
    store = Store(tmp_path/'state.db')
    rid = store.create(CollectionSpec(keywords=['test'], count=2, include_replies=True).model_dump(mode='json'))
    rows = [{'platform': 'twitter', 'tweet_id': '1', 'comments_status': 'finished', 'replies_data': [{'tweet_id': 'c'}]},
            {'platform': 'twitter', 'tweet_id': '2', 'comments_status': 'partial', 'comments_reason': 'unavailable'}]
    store.save(rid, 'unit', rows, {})
    store.progress(rid, {**comment_summary(rows), 'new_comments': 1})
    store.status(rid, 'running')
    monkeypatch.setattr(collection_api, 'run_active', lambda _: True)
    monkeypatch.setattr(store, 'rows', lambda _: pytest.fail('Polling must use compact stored summaries'))
    state = collection_api.summary(store, rid)
    assert state['status'] == 'running' and state['comments_pending'] == 1
    assert state['new_comments'] == 1 and state['comments_unavailable'] == 1
    assert 'csv_data' not in state and 'xlsx_data' not in state
    store.status(rid, 'partial')
    assert collection_api.summary(store, rid)['status'] == 'finishing'
    monkeypatch.setattr(collection_api, 'run_active', lambda _: False)
    assert collection_api.summary(store, rid)['status'] == 'partial'


async def test_x_retry_limit_includes_retained_comments(no_wait):
    page = ReplyPage([[1,2,3,4,5]])
    progress = {'replies_data':[{'tweet_id':'old'}], 'replies':20}
    rows = await TwitterCookieFetcher()._scrape_replies_page(page, page.url, 3, progress=progress)
    assert len(rows) == 3 and {r['tweet_id'] for r in rows} == {'old','1','2'}
    assert progress['comments_status'] == 'partial' and progress['comments_reason'] == 'limit'


def test_legacy_summary_initialization_never_overwrites_new_progress(tmp_path):
    store = Store(tmp_path/'state.db')
    rid = store.create(CollectionSpec(keywords=['test']).model_dump(mode='json'))
    store.progress(rid, {'saved_posts':100,'new_comments':700})
    actual = store.progress(rid, {'saved_posts':20,'new_comments':0}, only_if_missing=True)
    assert actual['saved_posts'] == 100 and actual['new_comments'] == 700
