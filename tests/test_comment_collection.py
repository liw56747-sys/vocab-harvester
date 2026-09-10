"""评论分页回归测试：使用确定的页面/HTTP 响应，无需平台账号。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.crawlers import twitter_url, reddit_crawler
from src.crawlers.twitter_url import TwitterCookieFetcher, _EXPAND_TWEETS_JS
from src.crawlers.reddit_crawler import RedditCookieFetcher
from src.crawlers.browser_manager import apply_request_interceptors


@pytest.fixture
def no_wait(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    return sleep


class ReplyPage:
    def __init__(self, batches, more=False, fail_at=None):
        self.batches = list(batches)
        self.rounds = 0
        self.more = more
        self.fail_at = fail_at
        self.url = 'https://x.com/author/status/999999'
        self.close = AsyncMock()
        self.route = AsyncMock()
        self.goto = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.set_default_timeout = lambda value: None

    async def evaluate(self, script, arg=None):
        if script == _EXPAND_TWEETS_JS:
            return 0
        if script == TwitterCookieFetcher._EXTRACT_REPLIES_JS:
            self.rounds += 1
            if self.rounds == self.fail_at:
                raise RuntimeError('page crashed')
            batch = self.batches.pop(0) if self.batches else []
            return [{'tweetId': str(i), 'text': f'评论 {i}'} for i in batch if str(i) not in arg]
        if script == TwitterCookieFetcher._CLICK_SHOW_MORE_REPLIES_JS:
            if self.more:
                self.more = False
                self.batches.insert(0, [888])
                return True
            return False
        assert script.startswith('window.scrollBy'), '必须逐屏滚动，不能跳过虚拟列表中的评论'


async def test_twitter_continues_past_old_two_round_and_fifty_reply_limits(no_wait):
    page = ReplyPage([range(i, i + 20) for i in range(1, 121, 20)])
    progress = {}
    replies = await TwitterCookieFetcher()._scrape_replies_page(page, page.url, progress=progress)
    assert len(replies) == 120
    assert page.rounds == 6 + twitter_url._TC.comment_max_stalls
    assert progress['comments_status'] == 'finished'
    assert len(json.loads(progress['replies_data'])) == 120


async def test_twitter_duplicate_pages_stop_and_more_replies_get_read(no_wait):
    page = ReplyPage([[1], [1], [1]], more=True)
    replies = await TwitterCookieFetcher()._scrape_replies_page(page, page.url)
    assert [r['tweet_id'] for r in replies] == ['1', '888']
    assert page.rounds < 15


async def test_twitter_limit_is_exact_and_reported(no_wait):
    page = ReplyPage([range(10)])
    progress = {}
    replies = await TwitterCookieFetcher()._scrape_replies_page(page, page.url, 3, progress=progress)
    assert len(replies) == 3
    assert progress['comments_status'] == 'partial'
    assert '上限' in progress['comments_warning']


async def test_twitter_page_failure_preserves_collected_replies(no_wait):
    page = ReplyPage([[1, 2]], fail_at=2)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    tweet = {'url': page.url}
    await TwitterCookieFetcher(block_resources=True)._parallel_scrape_replies(context, [tweet])
    assert tweet['replies_count'] == 2
    assert len(json.loads(tweet['replies_data'])) == 2
    assert tweet['comments_status'] == 'partial'
    assert 'page crashed' in tweet['comments_warning']
    page.close.assert_awaited_once()


async def test_twitter_retries_initial_navigation_failure(no_wait):
    first, second = ReplyPage([]), ReplyPage([[1]])
    first.goto.side_effect = RuntimeError('temporary network failure')
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[first, second]))
    tweet = {'url': second.url}
    await TwitterCookieFetcher()._parallel_scrape_replies(context, [tweet])
    assert tweet['replies_count'] == 1
    assert tweet['comments_warning'] == ''
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()


async def test_twitter_pending_comments_are_visible_after_cancellation(monkeypatch, no_wait):
    entered = asyncio.Event()
    async def stuck(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    fetcher = TwitterCookieFetcher()
    monkeypatch.setattr(fetcher, '_scrape_replies_page', stuck)
    context = SimpleNamespace(new_page=AsyncMock(side_effect=lambda: ReplyPage([])))
    tweets = [{'url': f'https://x.com/u/status/{i}'} for i in range(100)]
    task = asyncio.create_task(fetcher._parallel_scrape_replies(context, tweets))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(t['comments_warning'] for t in tweets)
    assert sum(t['comments_status'] == 'pending' for t in tweets) == 97


@pytest.mark.parametrize('resource_type,blocked', [('image', True), ('media', True), ('stylesheet', False), ('script', False), ('xhr', False), ('fetch', False)])
async def test_speed_mode_keeps_layout_and_comment_requests(resource_type, blocked):
    page = SimpleNamespace(route=AsyncMock())
    await apply_request_interceptors(page, block_resources=True, ct0_token='test-csrf')
    handler = page.route.call_args.args[1]
    route = SimpleNamespace(request=SimpleNamespace(resource_type=resource_type, headers={}),
                            abort=AsyncMock(), continue_=AsyncMock())
    await handler(route)
    assert route.abort.await_count == int(blocked)
    if not blocked:
        route.continue_.assert_awaited_once_with(headers={'x-csrf-token': 'test-csrf'})


def comment(cid, replies='', body=None):
    return {'kind': 't1', 'data': {'id': cid, 'body': body or f'comment {cid}',
                                  'parent_id': 't3_root', 'author': 'alice', 'replies': replies}}


def listing(children):
    return {'data': {'children': children}}


def more(ids):
    return {'kind': 'more', 'data': {'children': ids}}


async def test_reddit_expands_over_100_hidden_comments_and_nested_more(no_wait):
    batches = []
    long_body = '完整正文' * 1000
    def handler(request):
        if request.url.path.endswith('/comments/root.json'):
            return httpx.Response(200, json=[{}, listing([comment('first', body=long_body), more([f'c{i}' for i in range(105)])])])
        ids = request.url.params['children'].split(',')
        batches.append(ids)
        children = [comment(cid) for cid in ids]
        if ids[0] == 'c0':
            children += [comment('first'), more(['nested'])]
        return httpx.Response(200, json={'json': {'data': {'things': children}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        progress = {}
        comments = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root', progress=progress)
    assert len(comments) == 107
    assert max(map(len, batches)) <= 100
    assert len({c['comment_id'] for c in comments}) == 107
    assert comments[0]['content'] == long_body
    assert progress['comments_warning'] == ''


async def test_reddit_deep_thread_deduplicates_parent_but_keeps_new_child(no_wait):
    def handler(request):
        if request.url.params.get('comment'):
            return httpx.Response(200, json=[{}, listing([comment('parent', listing([comment('child')]))])])
        continuation = {'kind': 'more', 'data': {'children': [], 'parent_id': 't1_parent'}}
        return httpx.Response(200, json=[{}, listing([comment('parent', listing([continuation]))])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        comments = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root')
    assert [c['comment_id'] for c in comments] == ['parent', 'child']


@pytest.mark.parametrize('status', [429, 503])
async def test_reddit_retries_temporary_http_errors(no_wait, status):
    responses = [httpx.Response(status, headers={'Retry-After': '3'}), httpx.Response(200, json=[{}, listing([comment('ok')])])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: responses.pop(0))) as client:
        comments = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root')
    assert len(comments) == 1
    no_wait.assert_awaited_with(3)


async def test_reddit_more_failure_keeps_first_page_and_reports_warning(no_wait):
    def handler(request):
        if 'morechildren' in request.url.path:
            return httpx.Response(403)
        return httpx.Response(200, json=[{}, listing([comment('saved'), more(['blocked'])])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        progress = {}
        comments = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root', progress=progress)
    assert len(comments) == 1
    assert progress['comments_status'] == 'partial'
    assert '403' in progress['comments_warning']


async def test_reddit_auth_failure_is_not_reported_as_empty_success(no_wait):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root')


async def test_twitter_shortfall_is_reported_instead_of_claiming_all_replies(no_wait):
    page = ReplyPage([[1]])
    progress = {'replies': 10}
    await TwitterCookieFetcher()._scrape_replies_page(page, page.url, progress=progress)
    assert progress['comments_status'] == 'partial'
    assert '实际采集 1 条' in progress['comments_warning']


async def test_reddit_retry_exhaustion_is_bounded_and_preserves_first_page(no_wait):
    requests = []
    def handler(request):
        requests.append(request)
        if 'morechildren' in request.url.path:
            return httpx.Response(429)
        return httpx.Response(200, json=[{}, listing([comment('saved'), more(['blocked'])])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        progress = {}
        comments = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root', progress=progress)
    assert len(requests) == 1 + reddit_crawler._RC.comment_retry_attempts
    assert len(comments) == 1
    assert '429' in progress['comments_warning']


async def test_reddit_does_not_retry_before_long_retry_after(no_wait):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={'Retry-After': '120'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root')
    assert len(requests) == 1
    no_wait.assert_not_awaited()


async def test_reddit_limit_reported_without_overfetch(no_wait):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[{}, listing([comment(str(i)) for i in range(10)])])
    )) as client:
        progress = {}
        comments = await RedditCookieFetcher()._fetch_post_comments(client, 'test', 'root', 3, progress=progress)
    assert len(comments) == 3
    assert progress['comments_status'] == 'partial'
    assert '上限' in progress['comments_warning']
