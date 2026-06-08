"""SQLite Checkpointer - 零依赖模式替代 Postgres。

当 Postgres 不可用或 local_mode.enabled = true 时，
使用 LangGraph 内置的 MemorySaver 或 SQLite 作为 checkpointer。
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_sqlite_checkpointer():
    """获取 SQLite-based checkpointer。

    优先使用 langgraph-checkpoint-sqlite（如已安装），
    否则降级为 MemorySaver（内存持久化，重启丢失）。

    Returns:
        checkpointer 实例，或 None（如都不可用）
    """
    # 尝试 SQLite checkpointer
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        logger.info("使用 SQLite Checkpointer")
        return SqliteSaver.from_conn_string("autoreviewer_checkpoints.db")
    except ImportError:
        pass

    # 降级为内存 checkpointer
    try:
        from langgraph.checkpoint.memory import MemorySaver
        logger.info("SQLite 不可用，降级为 MemorySaver（重启丢失状态）")
        return MemorySaver()
    except ImportError:
        logger.warning("无可用的 checkpointer（MemorySaver 也未安装）")
        return None
