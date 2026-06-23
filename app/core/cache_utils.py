"""前缀缓存工具模块 - V3.0。

为支持 Prefix Caching 的 LLM 提供商（如 Claude 3.5 Sonnet、vLLM）
优化 System Prompt 缓存，实现成本下降和速度提升。

原理：
- System Prompt 通常包含大量固定内容（公司规范、Repo-Map 等）
- 每次请求只变化 Diff 部分
- 通过将 System Prompt 标记为 `ephemeral` 类型，提示 LLM 提供商缓存此部分
- 后续请求只需为增量 token 付费

兼容性：
- 仅在 LLM Provider 支持时生效（Anthropic、OpenAI 等）
- 不支持的 Provider 会自动忽略此标记，不会报错
"""

import logging
from typing import List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


def apply_prefix_cache_to_messages(
    messages: List[BaseMessage],
    enabled: bool = True,
) -> List[BaseMessage]:
    """将 SystemMessage 标记为可缓存的 ephemeral 类型。

    根据 Anthropic/OpenAI 的 Prefix Caching 规范：
    - 将 SystemMessage 的 content 包装为带有 cache_control 标记的格式
    - 其他消息（HumanMessage、AIMessage）保持不变

    Args:
        messages: 原始消息列表
        enabled: 是否启用缓存标记（配置开关）

    Returns:
        处理后的消息列表
    """
    if not enabled:
        return messages

    processed_messages: List[BaseMessage] = []
    cache_applied = False

    for msg in messages:
        if isinstance(msg, SystemMessage):
            # 将 SystemMessage 转换为带缓存标记的格式
            # 使用 Anthropic 的 cache_control 标记
            cached_msg = _wrap_with_cache_control(msg)
            processed_messages.append(cached_msg)
            cache_applied = True
        else:
            processed_messages.append(msg)

    if cache_applied:
        logger.debug("已应用前缀缓存标记到 SystemMessage")

    return processed_messages


def _wrap_with_cache_control(msg: SystemMessage) -> SystemMessage:
    """将 SystemMessage 包装为带缓存控制标记的格式。

    根据不同 LLM Provider 的规范：
    1. Anthropic Claude: 使用 `cache_control: {"type": "ephemeral"}` 标记
    2. OpenAI: 使用 `cache_control` 字段（实验性）
    3. vLLM: 支持类似格式

    为兼容性考虑，使用通用的消息结构。
    """
    # 方式1: 使用 LangChain 的标准格式（兼容大多数 Provider）
    # 将 content 转换为带有 cache_control 标记的结构
    # 注意：某些 Provider 可能忽略此标记，不影响正常功能

    if isinstance(msg.content, str):
        # 将字符串内容转换为带缓存标记的结构
        # 这是 Anthropic 的标准格式
        new_content = [
            {
                "type": "text",
                "text": msg.content,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        return SystemMessage(content=new_content, additional_kwargs=msg.additional_kwargs)

    # 如果已经是列表格式，添加 cache_control 标记
    if isinstance(msg.content, list):
        new_content = []
        for item in msg.content:
            if isinstance(item, dict) and "text" in item:
                new_item = {
                    **item,
                    "cache_control": {"type": "ephemeral"},
                }
                new_content.append(new_item)
            else:
                new_content.append(item)
        return SystemMessage(content=new_content, additional_kwargs=msg.additional_kwargs)

    # 其他格式保持不变
    return msg


def create_cached_system_message(content: str) -> SystemMessage:
    """创建带缓存标记的 SystemMessage。

    便捷函数，用于在 Agent 节点中直接创建缓存友好的 SystemMessage。

    Args:
        content: System Prompt 内容

    Returns:
        带缓存标记的 SystemMessage
    """
    return _wrap_with_cache_control(SystemMessage(content=content))


def estimate_cache_savings(
    system_prompt_tokens: int,
    diff_tokens: int,
    cache_hit_rate: float = 0.8,
) -> dict:
    """估算前缀缓存的成本节省。

    Args:
        system_prompt_tokens: System Prompt 的 token 数
        diff_tokens: Diff 内容的 token 数
        cache_hit_rate: 缓存命中率（默认 80%）

    Returns:
        节省估算字典
    """
    total_tokens = system_prompt_tokens + diff_tokens
    cached_tokens = int(system_prompt_tokens * cache_hit_rate)
    savings_rate = cached_tokens / total_tokens if total_tokens > 0 else 0

    return {
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "new_tokens": diff_tokens + (system_prompt_tokens - cached_tokens),
        "savings_rate": round(savings_rate * 100, 1),
        "note": "实际节省取决于 LLM Provider 的缓存实现和命中率",
    }
