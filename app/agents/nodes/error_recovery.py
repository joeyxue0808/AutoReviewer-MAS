"""错误恢复节点 - 处理 429/网络错误后的自动重试。

当 Reviewer 或 Fixer 的 LLM 调用失败时，
通过 error_recovery_node 进行指数退避后重试。
"""

import asyncio
import logging
import random
from typing import Any, Dict

from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)

# 最大连续错误次数，超过则强制降级
MAX_ERROR_COUNT = 3

# 基础退避时间（秒）
BASE_BACKOFF = 2.0


async def error_recovery_node(state: ReviewState) -> Dict[str, Any]:
    """错误恢复节点 — 处理 LLM 调用失败后的自动重试。

    策略：
    1. 检查 error_type（429 / timeout / connection）
    2. 计算指数退避时间 + jitter
    3. 等待后清除 error 标记，允许重试
    4. error_count >= MAX_ERROR_COUNT 时强制降级
    """
    error_count = state.get("error_count", 0)
    error_type = state.get("error_type", "unknown")
    last_node = state.get("last_node", "")

    logger.info(
        "Error Recovery: error_count=%d, error_type=%s, last_node=%s",
        error_count, error_type, last_node,
    )

    # 超过最大错误次数，强制降级
    if error_count >= MAX_ERROR_COUNT:
        logger.warning(
            "连续错误达到上限 (%d/%d)，强制降级到 submit",
            error_count, MAX_ERROR_COUNT,
        )
        return {
            "error_type": "",
            "last_node": "",
            # 保留 error_count，让 _after_tester 检测到并降级
        }

    # 指数退避 + jitter
    backoff = BASE_BACKOFF * (2 ** (error_count - 1))
    jitter = random.uniform(0, backoff * 0.5)
    wait_time = min(backoff + jitter, 30.0)  # 最大 30 秒

    logger.info("Error Recovery: 等待 %.1f 秒后重试 (node=%s)", wait_time, last_node)
    await asyncio.sleep(wait_time)

    # 清除 error 标记，允许重试
    return {
        "error_type": "",
        "last_node": "",
    }
