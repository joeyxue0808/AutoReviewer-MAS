"""FastAPI 启动入口 - Phase 1 生命周期管理。

在 lifespan 中管理 Redis 队列连接和 Postgres Checkpointer 初始化。
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.webhook import router as webhook_router
from app.api.approval import router as approval_router
from app.infra.queue import review_queue


def _setup_logging() -> None:
    """配置全局日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # 降低第三方库日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动时：连接 Redis 队列
    关闭时：断开 Redis 队列
    """
    _setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("AutoReviewer-MAS 启动中...")

    # 连接消息队列
    try:
        await review_queue.connect()
        logger.info("Redis 消息队列已连接")
    except Exception as e:
        logger.warning("Redis 队列连接失败（降级为无队列模式）: %s", e)

    yield

    # 关闭连接
    await review_queue.close()
    logger.info("AutoReviewer-MAS 关闭")


app = FastAPI(
    title="AutoReviewer-MAS",
    description="Multi-Agent System for Automated Code Review",
    version="0.4.0",
    lifespan=lifespan,
)

# 注册路由
app.include_router(webhook_router)
app.include_router(approval_router)


@app.get("/health")
async def health_check() -> dict:
    """健康检查端点。"""
    pending = await review_queue.pending()
    return {
        "status": "ok",
        "queue_pending": pending,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
