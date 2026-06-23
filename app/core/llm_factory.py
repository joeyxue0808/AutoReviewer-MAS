"""LLM 工厂模块。

提供 get_llm(role: str) 函数，根据角色返回对应的 LangChain ChatOpenAI 实例。
支持 Mimo 网关和 vLLM（均兼容 OpenAI API）。
集成 tenacity 库实现指数退避重试机制。
集成 Langfuse 全链路监控与 Token 记账 (Implementation Guide Phase 5 Task 5.2)。
"""

import asyncio
import logging
import os
import random
from typing import Any, Dict, List, Optional, Union

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from tenacity import (
    RetryCallState,
    retry,
    stop_after_attempt,
    wait_exponential,
)

# 并发信号量：限制同时进行的 LLM 请求数，防止触发网关限流
_LLM_SEMAPHORE = asyncio.Semaphore(8)

from app.core.cache_utils import apply_prefix_cache_to_messages
from app.core.config import settings
from app.infra.circuit_breaker import llm_breaker

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Langfuse 监控（可选依赖，优雅降级）
# ─────────────────────────────────────────────

_LANGFUSE_AVAILABLE = False
_langfuse_handler = None


def _init_langfuse():
    """初始化 Langfuse CallbackHandler（如果可用）。"""
    global _LANGFUSE_AVAILABLE, _langfuse_handler

    langfuse_secret = os.getenv("LANGFUSE_SECRET_KEY")
    langfuse_public = os.getenv("LANGFUSE_PUBLIC_KEY")

    if not langfuse_secret or not langfuse_public:
        logger.debug("Langfuse 未配置 (LANGFUSE_SECRET_KEY/LANGFUSE_PUBLIC_KEY)，跳过监控")
        return

    try:
        from langfuse.callback import CallbackHandler
        _langfuse_handler = CallbackHandler(
            secret_key=langfuse_secret,
            public_key=langfuse_public,
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        _LANGFUSE_AVAILABLE = True
        logger.info("Langfuse 监控已启用")
    except ImportError:
        logger.debug("langfuse 未安装，跳过监控。安装: pip install langfuse")
    except Exception as e:
        logger.warning("Langfuse 初始化失败: %s", e)


# 模块加载时尝试初始化
_init_langfuse()


def get_langfuse_callbacks(trace_id: Optional[str] = None) -> list:
    """获取 Langfuse 回调列表。

    Args:
        trace_id: 可选的 trace ID（如 mr_id），用于关联整个 Graph 执行

    Returns:
        回调列表（空列表表示未启用）
    """
    if not _LANGFUSE_AVAILABLE or not _langfuse_handler:
        return []

    if trace_id:
        # 为每次调用创建新的 handler 实例，关联到同一 trace
        try:
            from langfuse.callback import CallbackHandler
            return [CallbackHandler(
                secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
                public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
                host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
                trace_id=trace_id,
                metadata={"trace_id": trace_id},
            )]
        except Exception:
            return [_langfuse_handler]

    return [_langfuse_handler]


class RetryableChatModel(BaseChatModel):
    """带 tenacity 重试机制的 ChatModel 包装器。

    包装 ChatOpenAI 实例，在调用时自动应用指数退避重试。
    """

    _inner: ChatOpenAI
    _retry_decorator: Any

    _prefix_caching_enabled: bool

    def __init__(self, inner: ChatOpenAI, prefix_caching: bool = False, **kwargs: Any):
        retry_config = settings.llm.retry
        super().__init__(**kwargs)
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_prefix_caching_enabled", prefix_caching)
        object.__setattr__(
            self,
            "_retry_decorator",
            retry(
                stop=stop_after_attempt(retry_config.max_attempts),
                wait=self._adaptive_wait,
                reraise=True,
                before_sleep=self._log_retry,
            ),
        )

    @staticmethod
    def _log_retry(retry_state: RetryCallState) -> None:
        """重试前记录日志。"""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        exc_str = str(exc) if exc else "未知错误"
        # 429 错误降低日志级别（高频且预期）
        is_429 = exc and "429" in exc_str
        log_fn = logger.info if is_429 else logger.warning
        log_fn(
            "LLM 调用失败，正在重试 (第 %d 次): %s",
            retry_state.attempt_number,
            exc_str[:200],
        )

    @staticmethod
    def _adaptive_wait(retry_state: RetryCallState) -> float:
        """自适应退避策略：429 用更长等待 + jitter。"""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        attempt = retry_state.attempt_number
        is_429 = exc and "429" in str(exc)

        if is_429:
            # 429 限流：指数退避 4/8/16/32 秒 + 随机 jitter
            base = min(4 * (2 ** (attempt - 1)), 60)
            jitter = random.uniform(0, base * 0.5)
        else:
            # 其他错误：指数退避 2/4/8/16 秒 + jitter
            base = min(2 * (2 ** (attempt - 1)), 30)
            jitter = random.uniform(0, base * 0.3)

        wait = base + jitter
        logger.info("退避等待 %.1f 秒 (attempt=%d, is_429=%s)", wait, attempt, is_429)
        return wait

    @property
    def _llm_type(self) -> str:
        return "retryable-chat-openai"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """代理 bind_tools 到内部 ChatOpenAI 实例。"""
        return self._inner.bind_tools(tools, **kwargs)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        """代理 with_structured_output 到内部 ChatOpenAI 实例。"""
        return self._inner.with_structured_output(schema, **kwargs)

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        @self._retry_decorator
        def _inner_generate() -> Any:
            return self._inner._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

        return _inner_generate()

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        @self._retry_decorator
        async def _inner_agenerate() -> Any:
            async with _LLM_SEMAPHORE:
                # 应用前缀缓存标记（如果启用）
                processed_messages = apply_prefix_cache_to_messages(
                    messages,
                    enabled=self._prefix_caching_enabled,
                )
                return await self._inner._agenerate(
                    processed_messages, stop=stop, run_manager=run_manager, **kwargs
                )

        return await _inner_agenerate()

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return self._inner._identifying_params


# ─────────────────────────────────────────────
# 角色参数映射表 (Implementation Guide Phase 2 Task 2.2)
# ─────────────────────────────────────────────

# reviewer: 发散思维找 Bug，允许较高随机性
# fixer/tester: 强制消除随机性，保证代码与 JSON 严格收敛
_ROLE_PARAMS: dict[str, dict[str, Any]] = {
    "reviewer": {
        "temperature": 0.3,
        "top_p": 0.9,
    },
    "fixer": {
        "temperature": 0.0,
        "top_p": 1.0,
    },
    "tester": {
        "temperature": 0.0,
        "top_p": 1.0,
    },
}


# LLM 实例缓存（按 role 缓存，避免重复创建）
_LLM_CACHE: Dict[str, RetryableChatModel] = {}


def get_llm(role: str, trace_id: Optional[str] = None) -> RetryableChatModel:
    """根据角色获取对应的 LLM 实例，注入角色专属参数。

    参数策略（Implementation Guide Phase 2 Task 2.2）：
    - reviewer: temperature=0.3, top_p=0.9（发散思维找 Bug）
    - fixer:    temperature=0.0, top_p=1.0（严格收敛，消除随机性）
    - tester:   temperature=0.0, top_p=1.0（严格收敛，消除随机性）

    监控（Implementation Guide Phase 5 Task 5.2）：
    - 如果配置了 Langfuse 环境变量，自动注入 CallbackHandler
    - trace_id 关联整个 Graph 执行的 LLM 调用

    Args:
        role: LLM 角色名称 (reviewer, fixer, tester)
        trace_id: 可选的 trace ID（如 mr_id），用于 Langfuse 监控关联

    Returns:
        RetryableChatModel: 带重试机制的 ChatOpenAI 包装实例

    Raises:
        ValueError: 如果角色不存在于配置中
    """
    if role not in settings.llm.roles:
        raise ValueError(
            f"未知的 LLM 角色: '{role}'。可用角色: {list(settings.llm.roles.keys())}"
        )

    # 缓存命中：同一 role 复用同一 LLM 实例（配置不变则实例不变）
    if role in _LLM_CACHE:
        return _LLM_CACHE[role]

    role_config = settings.llm.roles[role]

    # 获取角色专属参数（覆盖配置文件中的默认值）
    role_params = _ROLE_PARAMS.get(role, {})

    # Langfuse 回调（Implementation Guide Phase 5 Task 5.2）
    callbacks = get_langfuse_callbacks(trace_id)

    # 创建 ChatOpenAI 实例
    # 兼容 Mimo 网关和 vLLM（均提供 OpenAI 兼容 API）
    chat_model = ChatOpenAI(
        base_url=role_config.base_url,
        model=role_config.model,
        api_key=role_config.api_key,
        temperature=role_params.get("temperature", role_config.temperature),
        top_p=role_params.get("top_p", 1.0),
        max_tokens=role_config.max_tokens,
        callbacks=callbacks if callbacks else None,
    )

    # 获取前缀缓存配置（默认关闭）
    prefix_caching_enabled = getattr(role_config, 'prefix_caching', False)

    logger.info(
        "已创建 LLM 实例: role=%s, model=%s, temp=%.1f, top_p=%.1f, prefix_caching=%s",
        role,
        role_config.model,
        role_params.get("temperature", role_config.temperature),
        role_params.get("top_p", 1.0),
        prefix_caching_enabled,
    )

    # 包装带重试机制的模型并缓存
    instance = RetryableChatModel(inner=chat_model, prefix_caching=prefix_caching_enabled)
    _LLM_CACHE[role] = instance
    return instance
