"""外部审核系统接入契约；不包含模型、提示词或内置审核规则。"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from src.common.models import ParsedPost, WorkflowResult


class AuditNotConfiguredError(RuntimeError):
    def __init__(self):
        super().__init__("审核服务尚未接入；采集数据可正常保存和导出")


class WorkflowAdapter(ABC):
    """外部审核实现应通过依赖注入接入，不从历史模型配置自动加载。"""

    @abstractmethod
    async def submit(self, posts: list[ParsedPost], *, context: dict[str, Any] | None = None) -> str:
        """提交数据并返回外部任务 ID；raw_data 可携带来源、父帖和媒体引用。"""
        ...

    @abstractmethod
    async def poll_result(self, task_id: str) -> WorkflowResult | None:
        """未完成返回 None，完成返回标准结果；失败应抛出异常。"""
        ...

    async def receive_callback(self, payload: dict) -> WorkflowResult:
        """可选回调解析契约；接入方负责签名验证与幂等，不默认开放 HTTP 回调。"""
        raise NotImplementedError("此适配器不支持回调模式")

    async def submit_and_wait(self, posts: list[ParsedPost], poll_interval: float = 2.0,
                              timeout: float = 300.0, *, context: dict[str, Any] | None = None) -> WorkflowResult:
        async def wait():
            task_id = await self.submit(posts, context=context)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                result = await self.poll_result(task_id)
                if result is not None:
                    return result
                await asyncio.sleep(poll_interval)
            raise TimeoutError(f"审核处理超时，任务 ID: {task_id}")
        return await asyncio.wait_for(wait(), timeout=timeout)
