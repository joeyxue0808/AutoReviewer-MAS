"""前缀缓存功能单元测试。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.cache_utils import (
    apply_prefix_cache_to_messages,
    create_cached_system_message,
    estimate_cache_savings,
)


class TestApplyPrefixCacheToMessages:
    """测试 apply_prefix_cache_to_messages 函数。"""

    def test_disabled_returns_original(self):
        """禁用时返回原始消息。"""
        messages = [
            SystemMessage(content="你是一个代码审查助手。"),
            HumanMessage(content="请审查代码。"),
        ]
        result = apply_prefix_cache_to_messages(messages, enabled=False)
        assert result == messages

    def test_system_message_gets_cache_control(self):
        """SystemMessage 应该被添加 cache_control 标记。"""
        messages = [
            SystemMessage(content="你是一个代码审查助手。"),
            HumanMessage(content="请审查代码。"),
        ]
        result = apply_prefix_cache_to_messages(messages, enabled=True)

        # SystemMessage 应该被转换为列表格式
        assert isinstance(result[0], SystemMessage)
        assert isinstance(result[0].content, list)
        assert result[0].content[0]["cache_control"] == {"type": "ephemeral"}
        assert result[0].content[0]["text"] == "你是一个代码审查助手。"

        # HumanMessage 应该保持不变
        assert isinstance(result[1], HumanMessage)
        assert result[1].content == "请审查代码。"

    def test_multiple_messages(self):
        """测试包含多个消息的场景。"""
        messages = [
            SystemMessage(content="系统提示。"),
            HumanMessage(content="用户消息1。"),
            AIMessage(content="AI回复。"),
            HumanMessage(content="用户消息2。"),
        ]
        result = apply_prefix_cache_to_messages(messages, enabled=True)

        # 只有 SystemMessage 被标记
        assert isinstance(result[0].content, list)
        assert "cache_control" in result[0].content[0]

        # 其他消息保持不变
        assert isinstance(result[1], HumanMessage)
        assert isinstance(result[2], AIMessage)
        assert isinstance(result[3], HumanMessage)

    def test_empty_messages(self):
        """测试空消息列表。"""
        result = apply_prefix_cache_to_messages([], enabled=True)
        assert result == []

    def test_no_system_message(self):
        """测试没有 SystemMessage 的场景。"""
        messages = [
            HumanMessage(content="用户消息。"),
        ]
        result = apply_prefix_cache_to_messages(messages, enabled=True)
        assert result == messages

    def test_list_content_format(self):
        """测试 SystemMessage 内容已经是列表格式的情况。"""
        messages = [
            SystemMessage(content=[{"type": "text", "text": "系统提示。"}]),
        ]
        result = apply_prefix_cache_to_messages(messages, enabled=True)

        assert isinstance(result[0].content, list)
        assert result[0].content[0]["cache_control"] == {"type": "ephemeral"}


class TestCreateCachedSystemMessage:
    """测试 create_cached_system_message 函数。"""

    def test_creates_message_with_cache_control(self):
        """创建的消息应该带有 cache_control 标记。"""
        msg = create_cached_system_message("系统提示。")
        assert isinstance(msg, SystemMessage)
        assert isinstance(msg.content, list)
        assert msg.content[0]["cache_control"] == {"type": "ephemeral"}
        assert msg.content[0]["text"] == "系统提示。"


class TestEstimateCacheSavings:
    """测试 estimate_cache_savings 函数。"""

    def test_basic_estimation(self):
        """测试基本的成本节省估算。"""
        result = estimate_cache_savings(
            system_prompt_tokens=1000,
            diff_tokens=500,
            cache_hit_rate=0.8,
        )
        assert result["total_tokens"] == 1500
        assert result["cached_tokens"] == 800
        assert result["new_tokens"] == 700
        assert result["savings_rate"] == 53.3

    def test_zero_tokens(self):
        """测试 token 数为零的情况。"""
        result = estimate_cache_savings(0, 0)
        assert result["total_tokens"] == 0
        assert result["savings_rate"] == 0

    def test_high_cache_hit_rate(self):
        """测试高缓存命中率的情况。"""
        result = estimate_cache_savings(2000, 100, cache_hit_rate=0.95)
        assert result["cached_tokens"] == 1900
        assert result["new_tokens"] == 200
