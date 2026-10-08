"""X 会话轮换、搜索失败分类及调用入口回归，无需真实账号。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.crawlers.browser_manager import BrowserPool, BrowserManager, apply_request_interceptors
from src.crawlers.twitter_url import TwitterCookieFetcher


async def test_cookie_attributes_allow_page_to_follow_csrf_rotation():
    context = SimpleNamespace(add_cookies=AsyncMock())
    pool = BrowserPool(size=1)
    pool._ensure_slot = AsyncMock()
    slot = pool.slots[0]
    slot.browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    slot.last_health = float('inf')
    slot.browser.is_connected = lambda: True
    await pool.new_context(cookies={'ct0': 'old-csrf', 'auth_token': 'session'})
    cookies = context.add_cookies.call_args.args[0]
    assert {c['domain'] for c in cookies} == {'.x.com', '.twitter.com'}
    assert all(c['secure'] for c in cookies)
    assert all(c['httpOnly'] == (c['name'] == 'auth_token') for c in cookies)


@pytest.mark.parametrize('speed', [False, True])
async def test_rotated_csrf_headers_are_preserved_and_never_sent_to_cdn(speed):
    page = SimpleNamespace(route=AsyncMock())
    await apply_request_interceptors(page, block_resources=speed, ct0_token='old-csrf')
    if not speed:
        page.route.assert_not_awaited()
        return
    handler = page.route.call_args.args[1]
    for url, headers in [
        ('https://x.com/i/api/graphql/id/SearchTimeline', {'x-csrf-token': 'rotated-csrf'}),
        ('https://x.com/i/api/graphql/id/TweetDetail', {'x-csrf-token': 'rotated-again'}),
        ('https://abs.twimg.com/app.js', {}),
        ('https://example.com/app.js', {}),
    ]:
        original = dict(headers)
        route = SimpleNamespace(request=SimpleNamespace(url=url, resource_type='fetch', headers=headers),
                                continue_=AsyncMock(), abort=AsyncMock())
        await handler(route)
        route.continue_.assert_awaited_once_with()
        assert headers == original


class FailedSearchPage:
    def __init__(self, status=None, empty=None, body='Loading', late_url=None, wait_error=None):
        self.url = 'https://x.com/search?q=login+workflow'
        self.listeners = {}
        self.status = status
        self.body = body
        self.late_url = late_url
        self.empty = empty
        self.goto = AsyncMock()
        self.reload = AsyncMock()
        self.close = AsyncMock()
        self.route = AsyncMock()
        self.screenshot = AsyncMock(side_effect=PlaywrightTimeoutError('screenshot stalled'))
        self.set_default_timeout = Mock()
        self.inner_text = AsyncMock(side_effect=lambda *a, **k: self.body)
        self.wait_error = wait_error or PlaywrightTimeoutError('waiting for tweets')
        self.wait_for_selector = AsyncMock(side_effect=self.wait)

    def on(self, event, handler):
        self.listeners[event] = handler

    def remove_listener(self, event, handler):
        assert self.listeners.pop(event) == handler

    async def wait(self, *args, **kwargs):
        if self.status:
            self.listeners['response'](SimpleNamespace(
                url='https://x.com/i/api/graphql/test/SearchTimeline?private=query', status=self.status))
        if self.late_url:
            self.url = self.late_url
        raise self.wait_error

    async def query_selector(self, selector):
        if selector == '[data-testid="emptyState"]' and self.empty:
            return SimpleNamespace(inner_text=AsyncMock(return_value=self.empty))
        return None


@pytest.fixture
def no_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    monkeypatch.setattr('os.path.expanduser', lambda value: str(tmp_path))


@pytest.mark.parametrize('status,body,late_url,expected', [
    (None, 'Loading', None, '未完成加载'),
    (403, 'Loading', None, '请求被拒绝'),
    (429, 'Loading', None, '限流'),
    (401, 'Loading', None, '会话已失效'),
    (503, 'Loading', None, '服务暂时不可用'),
    (None, 'Loading', 'https://x.com/i/flow/login', '会话已失效'),
])
async def test_failed_search_is_not_reported_as_empty(no_wait, status, body, late_url, expected):
    page = FailedSearchPage(status, body=body, late_url=late_url)
    with pytest.raises(RuntimeError, match=expected):
        await TwitterCookieFetcher()._scrape_search(page, 'login workflow', 5)
    assert not page.listeners
    assert page.screenshot.call_args.kwargs['timeout'] == 5000
    if status in {401, 403, 429}:
        page.reload.assert_not_awaited()


@pytest.mark.parametrize('empty', ['No results for "test"', '未找到相关结果', '没有结果'])
async def test_explicit_empty_state_remains_a_valid_empty_result(no_wait, empty):
    page = FailedSearchPage(status=200, empty=empty)
    assert await TwitterCookieFetcher()._scrape_search(page, 'test', 5) == []
    page.reload.assert_not_awaited()
    assert not page.listeners


async def test_no_results_words_without_empty_state_do_not_hide_loading_failure(no_wait):
    page = FailedSearchPage(body='Search for no results')
    with pytest.raises(RuntimeError, match='未完成加载'):
        await TwitterCookieFetcher()._scrape_search(page, 'no results', 5)


async def test_browser_crash_is_not_swallowed_as_timeout(no_wait):
    page = FailedSearchPage(wait_error=RuntimeError('browser closed'))
    with pytest.raises(RuntimeError, match='browser closed'):
        await TwitterCookieFetcher()._scrape_search(page, 'test', 5)
    page.reload.assert_not_awaited()
    assert not page.listeners


async def test_search_public_entry_propagates_failure_and_closes_context(no_wait, monkeypatch):
    page = FailedSearchPage(status=403)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    monkeypatch.setattr(BrowserManager, 'get', lambda: SimpleNamespace(new_context=AsyncMock(return_value=context)))
    with pytest.raises(RuntimeError, match='请求被拒绝'):
        await TwitterCookieFetcher().search_tweets('test', cookies={'ct0': 'old', 'auth_token': 'session'})
    page.close.assert_awaited_once()
    context.close.assert_awaited_once()


def test_scheduled_dual_platform_keeps_x_failure_and_other_platform_results(monkeypatch):
    from src.api.main import _scheduled_real_crawl
    import src.crawlers.real_crawler as crawlers
    monkeypatch.setattr(crawlers, 'crawl_twitter', AsyncMock(side_effect=RuntimeError('X 搜索请求被拒绝，请检查账号会话后重试')))
    monkeypatch.setattr(crawlers, 'crawl_reddit', AsyncMock(return_value=['reddit-result']))
    monkeypatch.setattr(BrowserManager, 'get', lambda: SimpleNamespace(close=AsyncMock()))
    result = _scheduled_real_crawl([('twitter', {}, None), ('reddit', {}, None)], ['test'], 5, 'top', False, True)
    assert result['posts'] == {'twitter': [], 'reddit': ['reddit-result']}
    assert '请求被拒绝' in result['errors']['twitter']
