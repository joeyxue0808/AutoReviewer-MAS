"""LangGraph 持久化检查点 - Phase 1 断点续传。

集成 langgraph-checkpoint-postgres，实现：
- Graph 流转每个节点自动将 ReviewState 落盘 Postgres
- Worker 崩溃后新 Worker 可从上次检查点恢复，无需重新消耗 Token
- 支持手动查询历史执行状态
"""

import logging
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# 延迟导入，仅在启用时加载
_postgres_saver = None


def get_checkpointer():
    """获取 Checkpointer 实例。

    降级策略：Postgres → SQLite → MemorySaver → None

    Returns:
        checkpointer 实例或 None
    """
    # 检查 local_mode 配置
    local_mode = getattr(settings, "local_mode", None)
    if local_mode and getattr(local_mode, "enabled", False):
        cp_type = getattr(local_mode, "checkpointer", "sqlite")
        if cp_type == "sqlite":
            return _get_sqlite_checkpointer()
        return None

    if not settings.checkpointer.enabled:
        logger.info("Checkpointer 未启用，使用内存模式")
        return None

    # 尝试 Postgres
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = AsyncPostgresSaver.from_conn_string(
            settings.checkpointer.postgres_url
        )
        logger.info("已创建 Postgres Checkpointer: url=%s", _mask_url(settings.checkpointer.postgres_url))
        return saver
    except ImportError:
        logger.warning(
            "langgraph-checkpoint-postgres 未安装，尝试 SQLite 降级"
        )
        return _get_sqlite_checkpointer()
    except Exception as e:
        logger.error("Postgres Checkpointer 初始化失败: %s，尝试 SQLite 降级", e)
        return _get_sqlite_checkpointer()


def _get_sqlite_checkpointer():
    """获取 SQLite checkpointer（零依赖降级方案）。"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        logger.info("使用 SQLite Checkpointer")
        return SqliteSaver.from_conn_string("autoreviewer_checkpoints.db")
    except ImportError:
        pass

    try:
        from langgraph.checkpoint.memory import MemorySaver
        logger.info("SQLite 不可用，降级为 MemorySaver（重启丢失状态）")
        return MemorySaver()
    except ImportError:
        logger.warning("无可用的 checkpointer")
        return None


async def setup_checkpointer_tables():
    """确保 Checkpointer 所需的 Postgres 表已创建。

    应在应用启动时调用一次。
    """
    if not settings.checkpointer.enabled:
        return

    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = AsyncPostgresSaver.from_conn_string(
            settings.checkpointer.postgres_url
        )
        # AsyncPostgresSaver.setup() 会自动创建所需的表
        async with saver as s:
            await s.setup()
        logger.info("Postgres Checkpointer 表结构已就绪")
    except Exception as e:
        logger.warning("Checkpointer 表初始化跳过: %s", e)


def _mask_url(url: str) -> str:
    """脱敏数据库连接字符串中的密码。"""
    if "@" in url and "://" in url:
        prefix = url.split("://")[0]
        rest = url.split("@", 1)[1]
        return f"{prefix}://***@{rest}"
    return url
