"""FastAPI 启动入口 - V3.0 生命周期管理。

在 lifespan 中管理 Redis 队列连接和 Checkpointer 初始化。
支持 local_mode 零依赖运行。
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.webhook import router as webhook_router
from app.api.approval import router as approval_router


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

    启动时：连接消息队列（Redis 或内存）
    关闭时：断开连接
    """
    _setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("AutoReviewer-MAS V3.0 启动中...")

    # 根据配置选择队列
    from app.infra.queue import create_queue
    queue = create_queue()

    try:
        await queue.connect()
        logger.info("消息队列已连接")
    except Exception as e:
        logger.warning("队列连接失败（降级为无队列模式）: %s", e)

    # 存储 queue 到 app state 供 health 端点使用
    app.state.queue = queue

    yield

    # 关闭连接
    await queue.close()
    logger.info("AutoReviewer-MAS 关闭")


app = FastAPI(
    title="AutoReviewer-MAS",
    description="Multi-Agent System for Automated Code Review",
    version="0.5.0",
    lifespan=lifespan,
)

# 注册路由
app.include_router(webhook_router)
app.include_router(approval_router)


@app.get("/health")
async def health_check() -> dict:
    """存活探针 — 进程是否在运行。"""
    return {"status": "ok"}


@app.get("/ready")
async def readiness_check() -> dict:
    """就绪探针 — 队列是否连通。"""
    queue = getattr(app.state, "queue", None)
    pending = 0
    queue_status = "disconnected"

    if queue:
        try:
            pending = await queue.pending()
            queue_status = "connected"
        except Exception:
            queue_status = "error"

    return {
        "status": "ok",
        "queue": queue_status,
        "queue_pending": pending,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
