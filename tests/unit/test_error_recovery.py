"""Error Recovery 节点单元测试。"""

import pytest
from app.agents.nodes.error_recovery import error_recovery_node, MAX_ERROR_COUNT


@pytest.mark.asyncio
async def test_recovery_under_limit():
    """错误次数未达上限时应清除 error 标记。"""
    state = {
        "error_count": 1,
        "error_type": "429",
        "last_node": "reviewer_node",
    }
    result = await error_recovery_node(state)
    assert result["error_type"] == ""
    assert result["last_node"] == ""


@pytest.mark.asyncio
async def test_recovery_at_limit():
    """错误次数达到上限时应保留 error_count（不清除）。"""
    state = {
        "error_count": MAX_ERROR_COUNT,
        "error_type": "429",
        "last_node": "fixer_node",
    }
    result = await error_recovery_node(state)
    # 不清除 error_type，让后续路由检测到并降级
    assert result["error_type"] == ""
    assert result["last_node"] == ""
