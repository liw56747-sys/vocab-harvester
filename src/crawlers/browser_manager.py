"""
BrowserManager — 共享 Playwright 浏览器实例管理（单例）

提供浏览器池化、健康检查、自动重启、网络拦截等功能。
所有 Twitter 抓取任务共享同一浏览器进程，通过独立 context 隔离 cookie。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class BrowserManager:
    """Playwright 浏览器生命周期管理（单例模式）"""

    _instance: Optional["BrowserManager"] = None
    _lock: Optional[asyncio.Lock] = None

    def __init__(self):
        self._pw = None
        self._browser = None
        self._proxy: Optional[str] = None

    @classmethod
    def get(cls) -> "BrowserManager":
        """获取单例（需在 async 上下文中调用）"""
        if cls._instance is None:
            cls._instance = cls()
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._instance

    # ── 核心方法 ──────────────────────────────────────────

    async def ensure_browser(self, proxy: str | None = None) -> "Browser":
        """确保浏览器存活，懒初始化 + 健康检查 + 自动重启"""
        async with self._lock:
            if self._browser is not None:
                try:
                    if self._browser.is_connected():
                        return self._browser
                except Exception:
                    pass
            await self._launch(proxy)
            return self._browser

    async def new_context(
        self,
        cookies: dict | None = None,
        proxy: str | None = None,
    ) -> "BrowserContext":
        """在共享浏览器上创建隔离的 context（独立 cookie 和会话）"""
        browser = await self.ensure_browser(proxy=proxy)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        if cookies:
            cookie_list = []
            for domain in [".x.com", ".twitter.com"]:
                cookie_list.append(
                    {"name": "ct0", "value": cookies.get("ct0", ""),
                     "domain": domain, "path": "/"}
                )
                cookie_list.append(
                    {"name": "auth_token", "value": cookies.get("auth_token", ""),
                     "domain": domain, "path": "/"}
                )
            await context.add_cookies(cookie_list)
        return context

    async def close(self):
        """关闭浏览器和 Playwright 实例（应用关闭时调用）"""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._proxy = None
        logger.info("BrowserManager: 浏览器已关闭")

    # ── 内部方法 ──────────────────────────────────────────

    async def _launch(self, proxy: str | None = None):
        """启动新的浏览器实例"""
        # 先关闭旧实例
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

        from playwright.async_api import async_playwright

        self._proxy = proxy
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
        )
        logger.info(f"BrowserManager: 浏览器已启动 (proxy={proxy or 'none'})")


# ── 网络拦截工具 ──────────────────────────────────────────

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "stylesheet", "font", "media"})


async def apply_resource_blocking(page):
    """拦截图片/CSS/字体/媒体请求，加速页面加载"""
    async def _handler(route):
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _handler)
