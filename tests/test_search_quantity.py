"""主帖数量与分页回归：检验真实抓取循环，不以 API 参数传递代替数量验证。"""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from src.crawlers import twitter_url, reddit_crawler
from src.crawlers.twitter_url import TwitterCookieFetcher
from src.crawlers.reddit_crawler import RedditCookieFetcher


class SearchPage:
    def __init__(self, batches):
        self.batches = list(batches)
        self.url = 'https://x.com/search?q=test'
        self.goto = AsyncMock()
        self.wait_for_selector = AsyncMock()
        self.query_selector = AsyncMock(return_value=None)
        self.inner_text = AsyncMock(return_value='Search results')
        self.rounds = 0

    async def evaluate(self, script, arg=None):
        if script == twitter_url._EXPAND_TWEETS_JS:
            return 0
        if script == twitter_url._EXTRACT_TWEETS_JS:
            self.rounds += 1
            ids = self.batches.pop(0) if self.batches else []
            return [{'tweet_id': str(i), 'content': f'post {i}'} for i in ids if str(i) not in arg['alreadySeenIds']]
        if script.startswith('window.scroll'):
            assert script.startswith('window.scrollBy')
            return None
        return False


@pytest.mark.parametrize('count,step', [(100, 20), (200, 1)])
@pytest.mark.parametrize('speed', [False, True])
async def test_twitter_search_reaches_requested_count_beyond_old_round_limits(monkeypatch, count, step, speed):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    page = SearchPage([range(i, i + step) for i in range(0, count, step)])
    rows = await TwitterCookieFetcher(block_resources=speed)._scrape_search(page, 'test', count)
    assert len(rows) == count
    assert len({r['tweet_id'] for r in rows}) == count
    assert page.rounds == count // step


async def test_twitter_delayed_and_duplicate_pages_do_not_end_search_early(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    page = SearchPage([range(10), [], range(10), range(10, 20)])
    rows = await TwitterCookieFetcher()._scrape_search(page, 'test', 20)
    assert len(rows) == 20
    assert page.rounds == 4


async def test_twitter_exhausted_results_stop_without_duplicates(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    page = SearchPage([[1, 2, 3]] * 20)
    rows = await TwitterCookieFetcher()._scrape_search(page, 'test', 100)
    assert len(rows) == 3
    assert page.rounds == 1 + twitter_url._TC.search_max_stalls


@pytest.mark.parametrize('count,page_size', [(150, 75), (15, 1)])
async def test_reddit_paginates_to_target_and_never_requests_over_100(monkeypatch, count, page_size):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    requests = []
    def handler(request):
        requests.append(request)
        start = int(request.url.params.get('after', '0'))
        children = [{'kind': 't3', 'data': {'id': str(i), 'title': 'title', 'subreddit': 'test'}}
                    for i in range(start, min(count, start + page_size))]
        after = str(start + page_size) if start + page_size < count else None
        return httpx.Response(200, json={'data': {'children': children, 'after': after}})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    rows, _ = await RedditCookieFetcher().search_posts('test', count=count, cookies={'reddit_session': 'fake'}, include_replies=False)
    assert len(rows) == count
    assert len(requests) == (count + page_size - 1) // page_size
    assert all(int(r.url.params['limit']) <= 100 for r in requests)


async def test_reddit_continues_duplicate_page_if_cursor_advances(monkeypatch):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    responses = [
        {'children': [{'data': {'id': 'one'}}], 'after': 'page2'},
        {'children': [{'data': {'id': 'one'}}], 'after': 'page3'},
        {'children': [{'data': {'id': 'two'}}], 'after': None},
    ]
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'data': responses.pop(0)})), **kwargs))
    rows, _ = await RedditCookieFetcher().search_posts('test', count=2, cookies={'reddit_session': 'fake'}, include_replies=False)
    assert [p['post_id'] for p in rows] == ['one', 'two']
