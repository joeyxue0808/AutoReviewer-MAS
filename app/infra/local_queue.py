"""进程内 asyncio.Queue 队列 - 零依赖模式替代 Redis Stream。

当 settings.local_mode.enabled = true 时，自动使用此队列替代 ReviewQueue。
无需安装 Redis，适用于本地开发和 CLI 模式。
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class LocalQueue:
    """基于 asyncio.Queue 的进程内消息队列。

    与 ReviewQueue 接口完全兼容，可无缝切换。
    注意：仅限单进程使用，不支持跨进程分发。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._pending: Dict[str, Dict[str, Any]] = {}

    async def connect(self) -> None:
        """内存队列无需连接，保持接口兼容。"""
        logger.info("LocalQueue 已初始化（内存模式）")

    async def close(self) -> None:
        """内存队列无需清理。"""
        pass

    async def publish(self, task: Dict[str, Any]) -> str:
        """发布任务到内存队列。

        Args:
            task: 任务 payload

        Returns:
            生成的消息 ID
        """
        message_id = str(uuid.uuid4())
        task_copy = dict(task)
        task_copy["_message_id"] = message_id
        await self._queue.put(task_copy)
        logger.info("LocalQueue: 任务已入队, id=%s", message_id)
        return message_id

    async def consume(self, count: int = 1, block_ms: int = 5000) -> List[Dict[str, Any]]:
        """从队列消费任务。

        Args:
            count: 单次最多消费的消息数
            block_ms: 阻塞等待时间（毫秒）

        Returns:
            消费到的任务列表
        """
        tasks = []
        timeout = block_ms / 1000.0

        for _ in range(count):
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                message_id = task.get("_message_id", "")
                if message_id:
                    self._pending[message_id] = task
                tasks.append(task)
            except asyncio.TimeoutError:
                break

        return tasks

    async def ack(self, message_id: str) -> None:
        """确认消息已处理完成。

        Args:
            message_id: 消息 ID
        """
        self._pending.pop(message_id, None)
        logger.debug("LocalQueue: 已确认消息 %s", message_id)

    async def pending(self) -> int:
        """查询待处理消息数量。"""
        return self._queue.qsize()


# 全局单例（与 review_queue 一致的使用模式）
local_queue = LocalQueue()
