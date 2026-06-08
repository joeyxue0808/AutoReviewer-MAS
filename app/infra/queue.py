"""Redis Stream 消息队列 - Phase 1 削峰重构。

废弃 FastAPI 脆弱的 BackgroundTasks，改用 Redis Stream 实现：
- Webhook 接收后立即将 ReviewState 推入队列
- Worker 进程阻塞消费队列，投递给 LangGraph 引擎
- 支持消费者组实现多 Worker 负载均衡
- Stream max_len 防止内存溢出
"""

import json
import logging
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)


class ReviewQueue:
    """基于 Redis Stream 的审查任务队列。

    生产者（Webhook）→ XADD → Redis Stream → XREADGROUP → 消费者（Worker）
    """

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self._stream_key = settings.queue.stream_key
        self._group = settings.queue.consumer_group
        self._consumer = settings.queue.consumer_name
        self._max_len = settings.queue.max_len

    async def connect(self) -> None:
        """建立 Redis 连接并确保消费者组存在。"""
        self._redis = aioredis.from_url(
            settings.queue.redis_url,
            decode_responses=True,
        )
        # 确保消费者组存在（幂等操作）
        try:
            await self._redis.xgroup_create(
                self._stream_key, self._group, id="0", mkstream=True
            )
            logger.info("已创建消费者组: %s", self._group)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug("消费者组已存在: %s", self._group)
            else:
                raise

    async def close(self) -> None:
        """关闭 Redis 连接。"""
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def publish(self, task: Dict[str, Any]) -> str:
        """发布审查任务到队列。

        Args:
            task: 序列化的 ReviewState 初始结构

        Returns:
            消息 ID
        """
        if not self._redis:
            raise RuntimeError("队列未连接，请先调用 connect()")

        message_id = await self._redis.xadd(
            self._stream_key,
            {"payload": json.dumps(task, ensure_ascii=False)},
            maxlen=self._max_len,
        )
        logger.info("已发布任务到队列: stream=%s, id=%s", self._stream_key, message_id)
        return message_id

    async def consume(self, count: int = 1, block_ms: int = 5000) -> list[Dict[str, Any]]:
        """从队列消费任务（阻塞式）。

        Args:
            count: 单次最多消费的消息数
            block_ms: 阻塞等待时间（毫秒）

        Returns:
            消费到的任务列表，每项包含 _message_id 和 payload
        """
        if not self._redis:
            raise RuntimeError("队列未连接，请先调用 connect()")

        results = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self._stream_key: ">"},
            count=count,
            block=block_ms,
        )

        tasks = []
        for stream, messages in results:
            for message_id, data in messages:
                payload = json.loads(data.get("payload", "{}"))
                payload["_message_id"] = message_id
                tasks.append(payload)

        return tasks

    async def ack(self, message_id: str) -> None:
        """确认消息已处理完成。

        Args:
            message_id: 消息 ID（来自 consume 返回的 _message_id）
        """
        if not self._redis:
            return
        await self._redis.xack(self._stream_key, self._group, message_id)
        logger.debug("已确认消息: %s", message_id)

    async def pending(self) -> int:
        """查询待处理消息数量。"""
        if not self._redis:
            return 0
        info = await self._redis.xpending(self._stream_key, self._group)
        return info.get("pending", 0) if info else 0


# 全局单例
review_queue = ReviewQueue()


def create_queue():
    """工厂函数：根据配置返回合适的队列实例。

    local_mode.enabled = true 时返回 LocalQueue（内存），
    否则返回 ReviewQueue（Redis Stream）。
    """
    local_mode = getattr(settings, "local_mode", None)
    if local_mode and getattr(local_mode, "enabled", False):
        from app.infra.local_queue import LocalQueue
        logger.info("Local mode: 使用内存队列")
        return LocalQueue()
    return review_queue
