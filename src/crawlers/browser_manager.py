"""
BrowserManager — Playwright 浏览器进程池管理

v1.5.3 改造要点：
- 单例改为进程池 (BrowserPool)，默认 N=2（macOS）/ 3（Windows/Linux）
- 每个实例带健康心跳 + 使用次数上限，超过即回收重建
- 高并发抓取时按 round-robin 分配，单实例挂死不影响其它任务
- 保留 `BrowserManager.get()`、`new_context()` 旧接口，业务代码无需改动
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ── 池配置 ────────────────────────────────────────────────

def _default_pool_size() -> int:
    """默认池大小：Mac 内存较紧，Windows/Linux 更大"""
    env = os.environ.get("VOCAB_BROWSER_POOL_SIZE", "").strip()
    if env.isdigit():
        return max(1, min(int(env), 6))
    return 2 if sys.platform == "darwin" else 3


_MAX_CONTEXTS_PER_BROWSER = int(os.environ.get("VOCAB_BROWSER_MAX_USES", "40"))
_HEALTH_CHECK_INTERVAL = 60.0  # 秒


class _BrowserSlot:
    """池中一个浏览器实例的元数据 + 生命周期管理"""

    __slots__ = ("index", "browser", "pw", "proxy", "use_count", "last_health", "lock", "_lock_loop")

    def __init__(self, index: int):
        self.index = index
        self.browser = None
        self.pw = None
        self.proxy: Optional[str] = None
        self.use_count = 0
        self.last_health = 0.0
        self.lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self.lock is None or self._lock_loop is not loop:
            self.lock = asyncio.Lock()
            self._lock_loop = loop
        return self.lock

    async def is_healthy(self) -> bool:
        """轻量健康检查：连接存活 + 使用次数未超限"""
        if self.browser is None:
            return False
        if self.use_count >= _MAX_CONTEXTS_PER_BROWSER:
            return False
        try:
            if not self.browser.is_connected():
                return False
        except Exception:
            return False
        # 定期主动 ping
        now = time.time()
        if now - self.last_health > _HEALTH_CHECK_INTERVAL:
            try:
                # contexts 属性访问是同步、极快的健康信号
                _ = self.browser.contexts
                self.last_health = now
            except Exception:
                return False
        return True

    async def launch(self, proxy: str | None):
        """启动一个新的浏览器实例（内部使用，需在锁内调用）"""
        await self._safe_close()

        from playwright.async_api import async_playwright

        self.proxy = proxy
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
        )
        self.use_count = 0
        self.last_health = time.time()
        logger.info(f"BrowserPool[{self.index}]: 启动 (proxy={proxy or 'none'})")

    async def _safe_close(self):
        """静默关闭，异常吞掉（回收时使用）"""
        if self.browser is not None:
            try:
                await asyncio.wait_for(self.browser.close(), timeout=10.0)
            except Exception:
                pass
            self.browser = None
        if self.pw is not None:
            try:
                await self.pw.stop()
            except Exception:
                pass
            self.pw = None

    async def close(self):
        await self._safe_close()
        self.proxy = None
        logger.info(f"BrowserPool[{self.index}]: 关闭")


class BrowserPool:
    """
    Playwright 浏览器进程池（跨事件循环安全）。

    对外通过 `new_context()` 提供隔离上下文；内部按 round-robin 派发到池成员。
    """

    _instance: Optional["BrowserPool"] = None

    def __init__(self, size: int | None = None):
        self.size = size or _default_pool_size()
        self.slots: list[_BrowserSlot] = [_BrowserSlot(i) for i in range(self.size)]
        self._rr_index = 0
        self._rr_lock: Optional[asyncio.Lock] = None
        self._rr_loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get(cls) -> "BrowserPool":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_test(cls, size: int = 2):
        """仅测试用：重置单例，允许注入不同池大小"""
        cls._instance = cls(size=size)

    def _get_rr_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._rr_lock is None or self._rr_loop is not loop:
            self._rr_lock = asyncio.Lock()
            self._rr_loop = loop
        return self._rr_lock

    async def _pick_slot(self) -> _BrowserSlot:
        """round-robin 选择一个 slot（原子递增）"""
        async with self._get_rr_lock():
            slot = self.slots[self._rr_index % self.size]
            self._rr_index = (self._rr_index + 1) % self.size
        return slot

    async def _ensure_slot(self, slot: _BrowserSlot, proxy: str | None) -> None:
        """确保 slot 内浏览器可用；不可用则回收重建"""
        async with slot.get_lock():
            if not await slot.is_healthy() or slot.proxy != proxy:
                # 代理变化或健康失败 → 重启
                try:
                    await asyncio.wait_for(slot.launch(proxy), timeout=60.0)
                except asyncio.TimeoutError as e:
                    logger.error(f"BrowserPool[{slot.index}]: 启动超时 60s")
                    raise RuntimeError("浏览器启动超时，请检查代理配置或重启应用") from e

    async def new_context(
        self,
        cookies: dict | None = None,
        proxy: str | None = None,
    ):
        """
        从池中取一个浏览器实例并创建隔离 context。

        Args:
            cookies: {"ct0": ..., "auth_token": ...}
            proxy: 可选代理 URL
        """
        last_err: Exception | None = None
        # 最多尝试 pool_size 次：某个 slot 挂了就换下一个
        for attempt in range(self.size):
            slot = await self._pick_slot()
            try:
                await self._ensure_slot(slot, proxy)
                async with slot.get_lock():
                    # 二次校验（拿锁后可能被别人回收）
                    if not await slot.is_healthy():
                        await asyncio.wait_for(slot.launch(proxy), timeout=60.0)
                    context = await asyncio.wait_for(
                        slot.browser.new_context(
                            viewport={"width": 1280, "height": 900},
                            user_agent=(
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/120.0.0.0 Safari/537.36"
                            ),
                        ),
                        timeout=30.0,
                    )
                    slot.use_count += 1

                if cookies:
                    ct0_value = cookies.get("ct0", "")
                    cookie_list = []
                    for domain in [".x.com", ".twitter.com"]:
                        cookie_list.append(
                            {"name": "ct0", "value": ct0_value,
                             "domain": domain, "path": "/",
                             "secure": True, "httpOnly": True, "sameSite": "None"}
                        )
                        cookie_list.append(
                            {"name": "auth_token", "value": cookies.get("auth_token", ""),
                             "domain": domain, "path": "/",
                             "secure": True, "httpOnly": True, "sameSite": "None"}
                        )
                    await context.add_cookies(cookie_list)
                return context
            except (asyncio.TimeoutError, RuntimeError, Exception) as e:
                last_err = e
                logger.warning(
                    f"BrowserPool[{slot.index}]: new_context 失败 (attempt {attempt+1}/{self.size}): {e}"
                )
                # 强制回收该 slot
                async with slot.get_lock():
                    await slot._safe_close()
                continue
        raise RuntimeError(f"浏览器池全部实例创建 context 失败: {last_err}")

    async def close_all(self):
        for slot in self.slots:
            try:
                await slot.close()
            except Exception:
                pass


# ── 向下兼容层：保留 BrowserManager 旧名字与 API ────────────

class BrowserManager:
    """
    向下兼容包装：将旧的单例 API 委托给 BrowserPool。
    业务代码 `BrowserManager.get().new_context(...)` 继续可用。
    """

    _instance: Optional["BrowserManager"] = None

    def __init__(self):
        self._pool = BrowserPool.get()

    @classmethod
    def get(cls) -> "BrowserManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure_browser(self, proxy: str | None = None):
        """保持旧签名。返回池中第一个健康的 browser（仅供极少数直接使用者）。"""
        slot = await self._pool._pick_slot()
        await self._pool._ensure_slot(slot, proxy)
        return slot.browser

    async def new_context(
        self,
        cookies: dict | None = None,
        proxy: str | None = None,
    ):
        return await self._pool.new_context(cookies=cookies, proxy=proxy)

    async def close(self):
        await self._pool.close_all()
        logger.info("BrowserManager: 池已全部关闭")


# ── 统一请求拦截 ──────────────────────────────────────────

_BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})


async def apply_request_interceptors(
    page,
    *,
    block_resources: bool = False,
    ct0_token: str = "",
):
    """
    统一注册请求拦截器：header 注入 + 可选的资源屏蔽。

    必须在 page 级别只调用一次，不能和 context.route() 混用，
    否则同 pattern 的 handler 会互相覆盖。

    Args:
        page: Playwright Page 对象
        block_resources: 是否屏蔽图片/字体/媒体请求（加速模式）
        ct0_token: Twitter ct0 cookie 值，用于注入 x-csrf-token 请求头
    """
    async def _handler(route):
        request = route.request

        # 1. 资源屏蔽（加速模式）
        if block_resources and request.resource_type in _BLOCKED_RESOURCE_TYPES:
            try:
                await route.abort()
            except Exception:
                pass
            return

        # 2. 注入 x-csrf-token 请求头（Twitter GraphQL API 必需）
        if ct0_token:
            headers = {**request.headers, "x-csrf-token": ct0_token}
            try:
                await route.continue_(headers=headers)
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass
        else:
            try:
                await route.continue_()
            except Exception:
                pass

    # 只在有实际拦截需求时才注册 route
    if block_resources or ct0_token:
        await page.route("**/*", _handler)
