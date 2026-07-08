"""工作流适配器基类"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.common.models import ParsedPost, WorkflowResult


class WorkflowAdapter(ABC):
    """与内部工作流对接的抽象适配器"""

    @abstractmethod
    async def submit(self, posts: list[ParsedPost], opinion_detail: str = "", opinion_rules: str = "") -> str:
        """
        将采集到的帖子数据投递给工作流。

        Args:
            posts: 采集到的帖子列表
            opinion_detail: 舆情事件详情
            opinion_rules: 舆情事件管控规则

        Returns:
            任务 ID，用于后续查询结果
        """
        ...

    @abstractmethod
    async def poll_result(self, task_id: str) -> WorkflowResult | None:
        """
        轮询工作流处理结果。

        Args:
            task_id: submit() 返回的任务 ID

        Returns:
            处理结果，若尚未完成则返回 None
        """
        ...

    async def receive_callback(self, payload: dict) -> WorkflowResult:
        """
        接收工作流的回调结果（可选实现）。

        用于工作流平台主动推送结果（webhook 模式）。
        默认实现抛出 NotImplementedError。

        Args:
            payload: 回调请求体

        Returns:
            解析后的工作流结果
        """
        raise NotImplementedError("此适配器不支持回调模式")

    async def submit_and_wait(self, posts: list[ParsedPost], poll_interval: float = 2.0, timeout: float = 300.0, opinion_detail: str = "", opinion_rules: str = "") -> WorkflowResult:
        """
        提交数据并等待结果（便捷方法）。

        Args:
            posts: 帖子列表
            poll_interval: 轮询间隔（秒）
            timeout: 超时时间（秒）
            opinion_detail: 舆情事件详情
            opinion_rules: 舆情事件管控规则

        Returns:
            工作流处理结果

        Raises:
            TimeoutError: 超过超时时间仍未获得结果
        """
        import asyncio
        import time

        task_id = await self.submit(posts, opinion_detail=opinion_detail, opinion_rules=opinion_rules)
        start = time.time()

        while time.time() - start < timeout:
            result = await self.poll_result(task_id)
            if result is not None:
                return result
            await asyncio.sleep(poll_interval)

        raise TimeoutError(f"工作流处理超时（{timeout}s），任务 ID: {task_id}")
