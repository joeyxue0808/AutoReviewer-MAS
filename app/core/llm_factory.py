"""LLM 工厂模块 - 严格遵循 Blueprint 规范。

提供 get_llm(role: str) 函数，根据角色返回对应的 LangChain ChatOpenAI 实例。
支持 Mimo 网关和 vLLM（均兼容 OpenAI API）。
集成 tenacity 库实现指数退避重试机制。
"""

import logging
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

from app.core.config import settings
from app.infra.circuit_breaker import llm_breaker

logger = logging.getLogger(__name__)


class RetryableChatModel(BaseChatModel):
    """带 tenacity 重试机制的 ChatModel 包装器。

    包装 ChatOpenAI 实例，在调用时自动应用指数退避重试。
    """

    _inner: ChatOpenAI
    _retry_decorator: Any

    def __init__(self, inner: ChatOpenAI, **kwargs: Any):
        retry_config = settings.llm.retry
        super().__init__(**kwargs)
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(
            self,
            "_retry_decorator",
            retry(
                stop=stop_after_attempt(retry_config.max_attempts),
                wait=wait_exponential(
                    multiplier=retry_config.multiplier,
                    min=retry_config.min_wait_seconds,
                    max=retry_config.max_wait_seconds,
                ),
                reraise=True,
                before_sleep=self._log_retry,
            ),
        )

    @staticmethod
    def _log_retry(retry_state: RetryCallState) -> None:
        """重试前记录日志。"""
        logger.warning(
            "LLM 调用失败，正在重试 (第 %d 次): %s",
            retry_state.attempt_number,
            retry_state.outcome.exception() if retry_state.outcome else "未知错误",
        )

    @property
    def _llm_type(self) -> str:
        return "retryable-chat-openai"

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
            # 熔断器保护：连续失败 N 次后触发熔断
            if settings.circuit_breaker.enabled:
                return await llm_breaker.call_async(
                    self._inner._agenerate(
                        messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                )
            return await self._inner._agenerate(
                messages, stop=stop, run_manager=run_manager, **kwargs
            )

        return await _inner_agenerate()

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return self._inner._identifying_params


def get_llm(role: str) -> RetryableChatModel:
    """根据角色获取对应的 LLM 实例。

    Args:
        role: LLM 角色名称 (reviewer, fixer, tester)

    Returns:
        RetryableChatModel: 带重试机制的 ChatOpenAI 包装实例

    Raises:
        ValueError: 如果角色不存在于配置中
    """
    if role not in settings.llm.roles:
        raise ValueError(
            f"未知的 LLM 角色: '{role}'。可用角色: {list(settings.llm.roles.keys())}"
        )

    role_config = settings.llm.roles[role]

    # 创建 ChatOpenAI 实例
    # 兼容 Mimo 网关和 vLLM（均提供 OpenAI 兼容 API）
    chat_model = ChatOpenAI(
        base_url=role_config.base_url,
        model=role_config.model,
        api_key=role_config.api_key,
        temperature=role_config.temperature,
        max_tokens=role_config.max_tokens,
    )

    logger.info(
        "已创建 LLM 实例: role=%s, model=%s, base_url=%s",
        role,
        role_config.model,
        role_config.base_url,
    )

    # 包装带重试机制的模型
    return RetryableChatModel(inner=chat_model)
