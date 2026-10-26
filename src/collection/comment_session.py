"""Task-local comment connections. Never shared between accounts or event loops."""
import asyncio
import logging
from contextlib import asynccontextmanager

from src.crawlers.comment_state import error_reason

logger = logging.getLogger(__name__)


class CommentOutcomeError(RuntimeError):
    """Keep an incomplete result distinct from a failed network request."""
    def __init__(self, post):
        super().__init__(post.get('comments_warning') or '评论未完成')
        self.reason = post.get('comments_reason') or error_reason(self)[0]


class CommentSession:
    # Bound context lifetime while retaining reuse across keywords and parents.
    CONTEXT_POST_LIMIT = 20

    def __init__(self, fetcher, platform, cookies):
        self.fetcher = fetcher
        self.platform = platform
        self.cookies = dict(cookies)
        self.context = None
        self.context_posts = 0
        self.idle_pages = []
        self.client = None
        self.pause_reason = ''
        self.denials = 0
        self.context_lock = asyncio.Lock()
        self.context_users = 0

    @asynccontextmanager
    async def twitter_lease(self):
        # A context may be recycled only after both active pages have returned.
        async with self.context_lock:
            context = await self.twitter_context()
            self.context_users += 1
        try:
            yield context
        finally:
            self.context_users -= 1

    async def twitter_context(self):
        if self.context is not None and self.context_posts >= self.CONTEXT_POST_LIMIT and not self.context_users:
            await self.close_context()
        if self.context is None:
            from src.crawlers.browser_manager import BrowserManager
            context = await BrowserManager.get().new_context(cookies=self.cookies, proxy=self.fetcher.proxy)
            self.context = context
            self.context_posts = 0
            # A crashed/closed context is discarded before the next parent.
            def closed(*_):
                if self.context is context:
                    self.context = None
                    self.idle_pages.clear()
            context.on('close', closed)
        self.context_posts += 1
        return self.context

    async def acquire_page(self, context):
        while self.idle_pages:
            page = self.idle_pages.pop()
            if not page.is_closed():
                return page
        from src.crawlers.browser_manager import apply_request_interceptors
        page = await context.new_page()
        try:
            # Install exactly once on each physical page; no duplicated handlers.
            await apply_request_interceptors(page, block_resources=self.fetcher.block_resources)
        except BaseException:
            await self.fetcher._safe_close(page, label='page(comment-setup)')
            raise
        return page

    async def release_page(self, page, reusable):
        if reusable and not page.is_closed() and len(self.idle_pages) < 2:
            try:
                # Stop the previous post's background traffic while waiting for the gate.
                await page.goto('about:blank', wait_until='commit', timeout=5000)
                self.idle_pages.append(page)
                return
            except Exception:
                pass
        await self.fetcher._safe_close(page, label='page(comment-session)')

    async def reddit_client(self):
        if self.client is None:
            import httpx
            from src.crawlers.platform_config import reddit_config
            self.client = httpx.AsyncClient(
                timeout=reddit_config.comment_request_timeout, follow_redirects=True,
                cookies=self.fetcher._session_cookies if self.fetcher._session_cookies is not None else self.cookies,
                headers=self.fetcher.request_headers(), proxy=self.fetcher.proxy,
            )
        return self.client

    def record(self, post):
        reason = post.get('comments_reason') or error_reason(post.get('comments_warning', ''))[0]
        if post.get('comments_status') == 'finished':
            self.denials = 0
            return
        self.denials = self.denials + 1 if reason == 'access_denied' else 0
        if reason in {'authentication', 'rate_limit'} or self.denials >= 3:
            self.pause_reason = reason

    def pause_post(self, post):
        reason = self.pause_reason
        if not reason and getattr(self.fetcher, 'comment_circuit_open', False):
            reason = 'access_denied'
        if not reason:
            return False
        messages = {
            'authentication': '登录会话不可用，本轮暂停该平台后续评论请求；更新 Cookie 后可补抓',
            'rate_limit': '平台已限流，本轮暂停该平台后续评论请求；等待限制解除后可补抓',
            'access_denied': '评论访问连续被拒绝，本轮暂停后续请求；检查会话和网络后可补抓',
        }
        status = 'partial' if post.get('replies_data') or post.get('comments_fetched') else ('failed' if post.get('comments_attempted') or post.get('comments_status') == 'failed' else 'pending')
        post.update(comments_status=status, comments_active=False,
                    comments_reason=reason, comments_warning=messages[reason])
        return True

    async def close_context(self):
        context, self.context = self.context, None
        self.context_posts = 0
        self.idle_pages.clear()
        if context is not None:
            await self.fetcher._safe_close(context, label='context(comment-session)')

    async def close(self):
        try:
            await self.close_context()
        finally:
            client, self.client = self.client, None
            if client is not None:
                try:
                    await asyncio.wait_for(client.aclose(), timeout=10)
                except Exception:
                    logger.warning('评论 HTTP 连接关闭失败或超时')
