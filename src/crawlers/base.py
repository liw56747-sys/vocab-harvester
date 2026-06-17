"""爬虫基类定义"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from src.common.models import CrawlQuery, ParsedPost


class BaseCrawler(ABC):
    """所有平台爬虫的抽象基类"""

    platform: str = ""

    def __init__(self, rate_limit: int = 5):
        self.rate_limit = rate_limit

    @abstractmethod
    async def fetch(self, query: CrawlQuery) -> list[ParsedPost]:
        """
        根据查询条件采集数据。

        Args:
            query: 采集查询参数

        Returns:
            解析后的帖子列表
        """
        ...

    async def fetch_stream(self, query: CrawlQuery) -> AsyncIterator[ParsedPost]:
        """
        流式采集数据，逐条返回（可选实现）。

        默认实现为一次性返回 fetch() 结果。
        """
        posts = await self.fetch(query)
        for post in posts:
            yield post

    async def close(self) -> None:
        """清理资源（如关闭 HTTP 客户端）"""
        pass
