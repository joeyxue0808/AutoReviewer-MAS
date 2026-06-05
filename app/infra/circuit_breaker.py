"""API 熔断器 - Phase 1 自适应限流。

基于 pybreaker 库实现熔断保护：
- 60 秒内连续 N 次 API 超时/502 → 触发熔断 (Open State)
- 熔断后等待 reset_timeout 秒 → 进入半开状态 (Half-Open)
- 半开状态下一次成功 → 恢复 (Closed State)
- 半开状态下一次失败 → 重新熔断

保护目标：LLM 网关 API 和 VCS API

降级策略：如果 pybreaker 未安装，熔断器退化为透传（不拦截任何调用）。
"""

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# pybreaker 可选依赖检测
# ─────────────────────────────────────────────

try:
    import pybreaker

    _PYBREAKER_AVAILABLE = True
except ImportError:
    _PYBREAKER_AVAILABLE = False
    logger.info("pybreaker 未安装，熔断器降级为透传模式。安装: pip install pybreaker")


# ─────────────────────────────────────────────
# 透传包装器（pybreaker 不可用时的降级方案）
# ─────────────────────────────────────────────


class _PassthroughBreaker:
    """透传熔断器：不拦截任何调用，仅做日志记录。"""

    def __init__(self, name: str):
        self.name = name
        self._fail_count = 0

    async def call_async(self, coro):
        """直接执行协程，不拦截。"""
        return await coro

    def __call__(self, func):
        """装饰器模式透传。"""
        return func

    @property
    def state(self):
        return "passthrough"


# ─────────────────────────────────────────────
# 全局熔断器实例
# ─────────────────────────────────────────────

if _PYBREAKER_AVAILABLE:

    class _CircuitBreakerListener(pybreaker.CircuitBreakerListener):
        """熔断器事件监听器，记录状态变化日志。"""

        def on_state_change(self, breaker, old_state, new_state):
            logger.warning(
                "熔断器状态变化: %s -> %s (name=%s)",
                old_state.name if hasattr(old_state, "name") else old_state,
                new_state.name if hasattr(new_state, "name") else new_state,
                breaker.name,
            )

        def on_success(self, breaker):
            pass

        def on_failure(self, breaker, exc):
            logger.warning(
                "熔断器记录失败: name=%s, exception=%s",
                breaker.name,
                type(exc).__name__,
            )

    _listener = _CircuitBreakerListener()

    def _build_excluded_exceptions() -> tuple[type[Exception], ...]:
        builtins = {
            "ValueError": ValueError,
            "KeyError": KeyError,
            "TypeError": TypeError,
            "RuntimeError": RuntimeError,
            "ConnectionError": ConnectionError,
            "TimeoutError": TimeoutError,
        }
        excluded = [
            builtins[name]
            for name in settings.circuit_breaker.exclude_exceptions
            if name in builtins
        ]
        return tuple(excluded) if excluded else (ValueError, KeyError)

    llm_breaker = pybreaker.CircuitBreaker(
        fail_max=settings.circuit_breaker.fail_max,
        reset_timeout=settings.circuit_breaker.reset_timeout,
        exclude=_build_excluded_exceptions(),
        name="llm_gateway",
        listeners=[_listener],
    )

    vcs_breaker = pybreaker.CircuitBreaker(
        fail_max=settings.circuit_breaker.fail_max,
        reset_timeout=settings.circuit_breaker.reset_timeout,
        exclude=_build_excluded_exceptions(),
        name="vcs_api",
        listeners=[_listener],
    )

else:
    # 降级：透传熔断器
    llm_breaker = _PassthroughBreaker("llm_gateway")
    vcs_breaker = _PassthroughBreaker("vcs_api")


async def llm_breaker_async(coro):
    """用 LLM 熔断器包装异步调用。"""
    return await llm_breaker.call_async(coro)


async def vcs_breaker_async(coro):
    """用 VCS 熔断器包装异步调用。"""
    return await vcs_breaker.call_async(coro)
