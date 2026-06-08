"""Critic 节点单元测试 - 纯规则检查，零外部依赖。"""

import pytest
from app.agents.nodes.critic import critic_node


@pytest.mark.asyncio
async def test_critic_empty_blocks():
    """无 block 时应直接返回空。"""
    state = {"search_replace_blocks": []}
    result = await critic_node(state)
    assert result == {}


@pytest.mark.asyncio
async def test_critic_valid_blocks():
    """合法 block 应通过审查。"""
    state = {
        "search_replace_blocks": [
            {
                "file_path": "main.go",
                "search_block": 'func main() {\n\tfmt.Println("hello")\n}',
                "replace_block": 'func main() {\n\tslog.Info("hello")\n}',
            }
        ]
    }
    result = await critic_node(state)
    assert result == {}  # 通过，无修改


@pytest.mark.asyncio
async def test_critic_rejects_short_search():
    """search 过短应被拒绝。"""
    state = {
        "search_replace_blocks": [
            {
                "file_path": "main.go",
                "search_block": "hi",
                "replace_block": "hello world",
            }
        ]
    }
    result = await critic_node(state)
    assert "search_replace_blocks" in result
    assert result["search_replace_blocks"] == []
    assert result["is_test_passed"] is False
    assert "Critic 拒绝" in result["test_logs"]


@pytest.mark.asyncio
async def test_critic_rejects_identical_search_replace():
    """search 和 replace 完全相同应被拒绝。"""
    state = {
        "search_replace_blocks": [
            {
                "file_path": "main.py",
                "search_block": "def hello():\n    print('hello')",
                "replace_block": "def hello():\n    print('hello')",
            }
        ]
    }
    result = await critic_node(state)
    assert result["search_replace_blocks"] == []
    assert "完全相同" in result["test_logs"]


@pytest.mark.asyncio
async def test_critic_rejects_unmatched_brackets():
    """括号不匹配应被拒绝。"""
    state = {
        "search_replace_blocks": [
            {
                "file_path": "main.py",
                "search_block": "def foo(x:\n    return x + 1",
                "replace_block": "def foo(x):\n    return x + 1",
            }
        ]
    }
    result = await critic_node(state)
    assert result["search_replace_blocks"] == []
    assert "括号" in result["test_logs"]


@pytest.mark.asyncio
async def test_critic_multiple_blocks_mixed():
    """多个 block 中只要有一个有问题就全部拒绝。"""
    state = {
        "search_replace_blocks": [
            {
                "file_path": "a.py",
                "search_block": "def valid_function():\n    return 42",
                "replace_block": "def valid_function():\n    return 43",
            },
            {
                "file_path": "b.py",
                "search_block": "x",
                "replace_block": "y",
            },
        ]
    }
    result = await critic_node(state)
    assert result["search_replace_blocks"] == []


@pytest.mark.asyncio
async def test_critic_warns_empty_replace():
    """replace 为空但 search 不为空时应记录警告（但不拒绝）。"""
    state = {
        "search_replace_blocks": [
            {
                "file_path": "main.py",
                "search_block": "def deprecated_function():\n    pass\n    return None",
                "replace_block": "",
            }
        ]
    }
    # 当前实现：空 replace 不触发拒绝，只记录 warning
    result = await critic_node(state)
    # 通过（因为 search 足够长且括号匹配）
    assert result == {}
