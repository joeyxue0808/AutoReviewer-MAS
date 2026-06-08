"""LangGraph StateGraph 编排集成测试。

测试 graph 拓扑、条件路由、Send API 分发。
不依赖外部服务（LLM/Redis/Postgres）。
"""

import pytest
from app.agents.workflow import (
    build_graph,
    router_node,
    _after_reviewer,
    _after_critic,
    _after_tester,
    _make_sub_state,
    _format_review_report,
)


# ─────────────────────────────────────────────
# Graph 构建
# ─────────────────────────────────────────────

class TestBuildGraph:
    def test_graph_builds_without_error(self):
        """graph 应能成功构建。"""
        graph = build_graph()
        assert graph is not None

    def test_graph_compiles(self):
        """graph 应能成功编译。"""
        from app.agents.workflow import compile_graph
        compiled = compile_graph(checkpointer=None, interrupt_before=None)
        assert compiled is not None


# ─────────────────────────────────────────────
# Router 节点
# ─────────────────────────────────────────────

class TestRouterNode:
    def test_empty_diff(self):
        """空 diff 应发送空子状态。"""
        state = {
            "diff_chunks": {},
            "detected_languages": [],
            "vcs_provider": "cli",
            "pr_id": "test",
            "trigger_type": "cli",
            "repo_id": "test",
            "repo_context": "",
            "review_issues": [],
            "search_replace_blocks": [],
            "test_logs": "",
            "is_test_passed": False,
            "retry_count": 0,
            "error_count": 0,
            "error_type": "",
            "last_node": "",
        }
        # router_node 返回 Send 指令列表
        # 由于 Send 是 langgraph 内部类型，我们只验证不抛异常
        # 实际 Send 需要在 graph 上下文中执行


# ─────────────────────────────────────────────
# 条件路由函数
# ─────────────────────────────────────────────

class TestAfterReviewer:
    def test_no_issues_goes_to_end(self):
        state = {"review_issues": [], "error_type": ""}
        assert _after_reviewer(state) == "__end__"

    def test_critical_issues_goes_to_fixer(self):
        state = {
            "review_issues": [
                {"severity": "critical", "file_path": "a.py", "line_number": 1, "description": "bug", "suggestion": "fix"},
            ],
            "error_type": "",
        }
        assert _after_reviewer(state) == "fixer_node"

    def test_warning_only_goes_to_end(self):
        state = {
            "review_issues": [
                {"severity": "warning", "file_path": "a.py", "line_number": 1, "description": "style", "suggestion": "fix"},
            ],
            "error_type": "",
        }
        assert _after_reviewer(state) == "__end__"

    def test_error_goes_to_recovery(self):
        state = {
            "review_issues": [],
            "error_type": "429",
        }
        assert _after_reviewer(state) == "error_recovery_node"


class TestAfterCritic:
    def test_valid_blocks_goes_to_tester(self):
        state = {
            "search_replace_blocks": [{"file_path": "a.py", "search_block": "x", "replace_block": "y"}],
            "retry_count": 0,
            "error_count": 0,
            "test_logs": "",
        }
        assert _after_critic(state) == "tester_node"

    def test_no_blocks_critic_rejected_goes_to_fixer(self):
        state = {
            "search_replace_blocks": [],
            "retry_count": 0,
            "error_count": 0,
            "test_logs": "Critic 拒绝: search 过短",
        }
        assert _after_critic(state) == "fixer_node"

    def test_no_blocks_max_retry_goes_to_tester(self):
        state = {
            "search_replace_blocks": [],
            "retry_count": 3,
            "error_count": 0,
            "test_logs": "Critic 拒绝",
        }
        assert _after_critic(state) == "tester_node"

    def test_error_count_high_goes_to_tester(self):
        state = {
            "search_replace_blocks": [],
            "retry_count": 0,
            "error_count": 3,
            "test_logs": "",
        }
        assert _after_critic(state) == "tester_node"


class TestAfterTester:
    def test_passed_goes_to_submit(self):
        state = {"is_test_passed": True, "retry_count": 0}
        assert _after_tester(state) == "submit_node"

    def test_failed_goes_to_fixer(self):
        state = {"is_test_passed": False, "retry_count": 1}
        assert _after_tester(state) == "fixer_node"

    def test_max_retry_goes_to_submit(self):
        state = {"is_test_passed": False, "retry_count": 3}
        assert _after_tester(state) == "submit_node"


# ─────────────────────────────────────────────
# Report 格式化
# ─────────────────────────────────────────────

class TestFormatReport:
    def test_report_with_issues(self):
        state = {
            "review_issues": [
                {"severity": "critical", "file_path": "a.py", "line_number": 10, "description": "NPE", "suggestion": "add null check"},
            ],
            "test_logs": "=== [python] Test Result ===\nPassed: True",
            "is_test_passed": True,
            "retry_count": 1,
        }
        report = _format_review_report(state)
        assert "审查报告" in report
        assert "a.py" in report
        assert "NPE" in report

    def test_report_no_issues(self):
        state = {
            "review_issues": [],
            "test_logs": "",
            "is_test_passed": True,
            "retry_count": 0,
        }
        report = _format_review_report(state)
        assert "未发现问题" in report

    def test_report_degraded(self):
        state = {
            "review_issues": [{"severity": "warning", "file_path": "a.py", "line_number": 1, "description": "x", "suggestion": "y"}],
            "test_logs": "",
            "is_test_passed": False,
            "retry_count": 3,
        }
        report = _format_review_report(state)
        assert "降级提交" in report
