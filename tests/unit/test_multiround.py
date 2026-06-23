"""多轮审查功能单元测试。"""

import pytest
from unittest.mock import patch

from app.schemas.user_input import (
    UserActionType,
    UserInput,
    UserDecision,
    parse_user_input,
)
from app.agents.nodes.decision import make_decision, after_critic, after_reviewer_multiround
from app.agents.nodes.user_checkpoint import (
    user_checkpoint_node,
    after_user_checkpoint,
    _generate_issue_summary,
    _process_user_input,
    _process_complex_instruction,
)
from app.schemas.state import ReviewState


class TestUserInputParsing:
    """测试用户输入解析。"""

    def test_parse_approve_commands(self):
        """测试解析批准命令。"""
        approve_commands = ["y", "yes", "是", "确认", "approve", "批准"]
        for cmd in approve_commands:
            result = parse_user_input(cmd)
            assert result.action == UserActionType.APPROVE
            assert result.timestamp > 0

    def test_parse_reject_commands(self):
        """测试解析拒绝命令。"""
        reject_commands = ["n", "no", "否", "拒绝", "reject"]
        for cmd in reject_commands:
            result = parse_user_input(cmd)
            assert result.action == UserActionType.REJECT
            assert result.timestamp > 0

    def test_parse_stop_commands(self):
        """测试解析停止命令。"""
        stop_commands = ["stop", "停止", "exit", "退出", "quit"]
        for cmd in stop_commands:
            result = parse_user_input(cmd)
            assert result.action == UserActionType.STOP
            assert result.timestamp > 0

    def test_parse_skip_commands(self):
        """测试解析跳过命令。"""
        skip_commands = ["skip", "跳过", "next", "下一轮"]
        for cmd in skip_commands:
            result = parse_user_input(cmd)
            assert result.action == UserActionType.SKIP_ROUND
            assert result.timestamp > 0

    def test_parse_ignore_commands(self):
        """测试解析忽略命令。"""
        result = parse_user_input("忽略性能问题")
        assert result.action == UserActionType.IGNORE_ISSUES
        assert "性能" in result.content

    def test_parse_focus_commands(self):
        """测试解析关注命令。"""
        result = parse_user_input("只关注安全问题")
        assert result.action == UserActionType.FOCUS_ISSUES
        assert "安全" in result.content

    def test_parse_instruction_commands(self):
        """测试解析指令命令。"""
        result = parse_user_input("请检查文件a.py的安全性")
        assert result.action == UserActionType.INSTRUCTION
        assert "请检查文件a.py的安全性" == result.content


class TestUserDecision:
    """测试用户决策模型。"""

    def test_create_user_decision(self):
        """测试创建用户决策。"""
        decision = UserDecision(
            approved=True,
            instructions="修复所有问题",
            ignore_categories=["style"],
            focus_categories=["security", "bug"],
            max_rounds_override=5,
        )
        
        assert decision.approved is True
        assert decision.instructions == "修复所有问题"
        assert "style" in decision.ignore_categories
        assert "security" in decision.focus_categories
        assert decision.max_rounds_override == 5

    def test_default_values(self):
        """测试默认值。"""
        decision = UserDecision(approved=False)
        assert decision.approved is False
        assert decision.instructions is None
        assert decision.ignore_categories == []
        assert decision.focus_categories == []
        assert decision.max_rounds_override is None


class TestDecisionLogic:
    """测试决策逻辑。"""

    def test_make_decision_max_rounds_reached(self):
        """测试达到最大轮次时停止。"""
        state = {
            "current_round": 3,
            "max_rounds": 3,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "user_instructions": "",
            "fixed_issues": [],
            "round_reports": [],
        }
        
        result = make_decision(state)
        assert result == "submit_node"

    def test_make_decision_no_remaining_issues(self):
        """测试没有剩余问题时停止。"""
        state = {
            "current_round": 1,
            "max_rounds": 3,
            "remaining_issues": [],
            "user_instructions": "",
            "fixed_issues": [{"description": "已修复"}],
            "round_reports": [],
        }
        
        result = make_decision(state)
        assert result == "submit_node"

    def test_make_decision_user_stop_command(self):
        """测试用户停止指令。"""
        state = {
            "current_round": 1,
            "max_rounds": 3,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "user_instructions": "停止",
            "fixed_issues": [],
            "round_reports": [],
        }
        
        result = make_decision(state)
        assert result == "submit_node"

    def test_make_decision_continue(self):
        """测试继续下一轮。"""
        state = {
            "current_round": 1,
            "max_rounds": 3,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "user_instructions": "继续修复",
            "fixed_issues": [],
            "round_reports": [{"issues_count": 1}],
        }
        
        result = make_decision(state)
        assert result == "reviewer_node"

    def test_make_decision_convergence(self):
        """测试收敛检测。"""
        state = {
            "current_round": 2,
            "max_rounds": 3,
            "remaining_issues": [{"severity": "warning", "description": "问题1"}],
            "user_instructions": "",
            "fixed_issues": [],
            "round_reports": [
                {"issues_count": 0},
                {"issues_count": 0},
            ],
        }
        
        result = make_decision(state)
        assert result == "submit_node"

    def test_after_critic_with_critical_issues(self):
        """测试批评节点发现 critical 问题。"""
        state = {
            "review_issues": [
                {"severity": "critical", "description": "严重问题"},
                {"severity": "warning", "description": "警告"},
            ],
        }
        
        result = after_critic(state)
        assert result == "fixer_node"

    def test_after_critic_no_critical_issues(self):
        """测试批评节点没有 critical 问题。"""
        state = {
            "review_issues": [
                {"severity": "warning", "description": "警告"},
                {"severity": "info", "description": "信息"},
            ],
        }
        
        result = after_critic(state)
        assert result == "decision_node"

    def test_after_reviewer_multiround_with_error(self):
        """测试审查节点出错。"""
        state = {
            "error_type": "429",
            "review_issues": [],
        }
        
        result = after_reviewer_multiround(state)
        assert result == "error_recovery_node"

    def test_after_reviewer_multiround_with_critical_issues(self):
        """测试审查节点发现 critical 问题。"""
        state = {
            "error_type": "",
            "review_issues": [
                {"severity": "critical", "description": "严重问题"},
            ],
        }
        
        result = after_reviewer_multiround(state)
        assert result == "fixer_node"

    def test_after_reviewer_multiround_no_critical_issues(self):
        """测试审查节点没有 critical 问题。"""
        state = {
            "error_type": "",
            "review_issues": [
                {"severity": "warning", "description": "警告"},
            ],
        }
        
        result = after_reviewer_multiround(state)
        assert result == "user_checkpoint_node"


class TestUserCheckpointNode:
    """测试用户检查点节点。"""

    def test_user_checkpoint_auto_approve(self):
        """测试自动批准模式。"""
        state = {
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
            "auto_approve": True,
            "remaining_issues": [],
            "review_issues": [{"severity": "warning", "category": "bug", "description": "test"}],
            "pending_user_approval": False,
        }
        
        result = user_checkpoint_node(state)
        assert result["pending_user_approval"] is False
        assert result["user_approval_result"] is True
        assert "remaining_issues" in result

    def test_user_checkpoint_interrupt_mechanism(self):
        """测试 interrupt 机制：非自动模式下会调用 interrupt。"""
        state = {
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
            "auto_approve": False,
            "remaining_issues": [],
            "review_issues": [],
            "pending_user_approval": False,
        }
        
        # interrupt 会在非 Graph 上下文中抛出 RuntimeError
        with pytest.raises(RuntimeError):
            user_checkpoint_node(state)

    def test_user_checkpoint_process_approve(self):
        """测试 _process_user_input 处理批准指令。"""
        state = {
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
            "review_issues": [{"severity": "warning", "category": "bug", "description": "test"}],
            "remaining_issues": [],
        }
        
        user_input = UserInput(action=UserActionType.APPROVE, timestamp=0)
        result = _process_user_input(user_input, state)
        assert result["user_approval_result"] is True
        assert result["pending_user_approval"] is False
        assert "remaining_issues" in result

    def test_after_user_checkpoint_approved_with_issues(self):
        """测试用户批准后还有问题。"""
        state = {
            "user_approval_result": True,
            "pending_user_approval": False,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
        }
        
        result = after_user_checkpoint(state)
        assert result == "fixer_node"

    def test_after_user_checkpoint_approved_no_issues(self):
        """测试用户批准后没有问题。"""
        state = {
            "user_approval_result": True,
            "pending_user_approval": False,
            "remaining_issues": [],
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
        }
        
        result = after_user_checkpoint(state)
        assert result == "submit_node"

    def test_after_user_checkpoint_rejected_with_issues(self):
        """测试用户拒绝后还有问题。"""
        state = {
            "user_approval_result": False,
            "pending_user_approval": False,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
        }
        
        result = after_user_checkpoint(state)
        assert result == "reviewer_node"

    def test_after_user_checkpoint_rejected_no_issues(self):
        """测试用户拒绝后没有问题。"""
        state = {
            "user_approval_result": False,
            "pending_user_approval": False,
            "remaining_issues": [],
            "current_round": 0,
            "max_rounds": 3,
            "user_instructions": "",
        }
        
        result = after_user_checkpoint(state)
        assert result == "submit_node"

    def test_after_user_checkpoint_stop_command(self):
        """测试用户停止指令。"""
        state = {
            "user_approval_result": None,
            "pending_user_approval": False,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "current_round": 1,
            "max_rounds": 3,
            "user_instructions": "停止",
        }
        
        result = after_user_checkpoint(state)
        assert result == "submit_node"

    def test_after_user_checkpoint_skip_command(self):
        """测试用户跳过指令。"""
        state = {
            "user_approval_result": None,
            "pending_user_approval": False,
            "remaining_issues": [{"severity": "critical", "description": "问题1"}],
            "current_round": 1,
            "max_rounds": 3,
            "user_instructions": "跳过",
        }
        
        result = after_user_checkpoint(state)
        assert result == "reviewer_node"


class TestUserCheckpointCategoryExtraction:
    """测试问题类别提取。"""

    def test_extract_categories_from_text(self):
        """测试从文本中提取问题类别。"""
        from app.agents.nodes.user_checkpoint import _extract_categories
        
        # 测试安全类别
        assert "security" in _extract_categories("安全问题")
        assert "security" in _extract_categories("security issue")
        
        # 测试性能类别
        assert "performance" in _extract_categories("性能问题")
        assert "performance" in _extract_categories("performance issue")
        
        # 测试 bug 类别
        assert "bug" in _extract_categories("bug")
        assert "bug" in _extract_categories("错误")
        
        # 测试风格类别
        assert "style" in _extract_categories("风格问题")
        assert "style" in _extract_categories("style issue")
        
        # 测试无匹配
        assert "general" in _extract_categories("其他问题")
        
        # 测试多类别
        categories = _extract_categories("安全和性能问题")
        assert "security" in categories
        assert "performance" in categories


class TestComplexInstructionProcessing:
    """测试复杂指令解析与处理。"""

    def _create_sample_state(self, issues=None):
        """创建示例状态。"""
        if issues is None:
            issues = [
                {"severity": "critical", "category": "security", "file_path": "auth.py", "description": "SQL注入风险"},
                {"severity": "warning", "category": "performance", "file_path": "query.py", "description": "N+1查询"},
                {"severity": "info", "category": "style", "file_path": "utils.py", "description": "命名不规范"},
                {"severity": "critical", "category": "bug", "file_path": "auth.py", "description": "空指针异常"},
            ]
        return {
            "current_round": 1,
            "max_rounds": 3,
            "review_issues": issues,
            "remaining_issues": issues,
            "user_instructions": "",
        }

    def test_ignore_security_issues(self):
        """测试忽略安全类别问题。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("忽略安全问题", state)
        
        remaining = result.get("remaining_issues", [])
        assert len(remaining) == 3
        # 安全问题应被过滤
        for issue in remaining:
            assert issue.get("category") != "security"
        assert "user_instructions" in result

    def test_focus_performance_issues(self):
        """测试只关注性能问题。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("只关注性能问题", state)
        
        remaining = result.get("remaining_issues", [])
        assert len(remaining) == 1
        assert remaining[0].get("category") == "performance"

    def test_stop_command(self):
        """测试停止指令。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("停止", state)
        
        assert result.get("user_approval_result") is False
        assert result.get("remaining_issues") == []
        assert result.get("current_round") == 3  # 强制达到最大轮次

    def test_continue_command(self):
        """测试继续指令。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("继续修复", state)
        
        assert result.get("user_approval_result") is True
        assert len(result.get("remaining_issues", [])) == 4

    def test_set_max_rounds(self):
        """测试设置最大轮次。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("最多修2次", state)
        
        assert result.get("max_rounds") == 2

    def test_file_specific_instruction(self):
        """测试针对特定文件的指令 — 识别为通用文件指令。"""
        state = self._create_sample_state()
        # "检查 auth.py 的安全性" 会被解析为 focus + security 类别
        # 因为 "安全" 匹配 security 类别
        result = _process_complex_instruction("只看 auth.py", state)
        
        remaining = result.get("remaining_issues", [])
        # 应该只保留 auth.py 的问题
        for issue in remaining:
            assert issue.get("file_path") == "auth.py"

    def test_ignore_all_command(self):
        """测试忽略全部问题。"""
        state = self._create_sample_state()
        # 使用更明确的忽略指令
        result = _process_complex_instruction("忽略全部问题", state)
        
        assert result.get("remaining_issues") == []

    def test_complex_mixed_instruction(self):
        """测试复杂混合指令 — 忽略风格问题。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("忽略风格问题", state)
        
        remaining = result.get("remaining_issues", [])
        # 应该过滤掉 style 类别
        for issue in remaining:
            assert issue.get("category") != "style"
        assert len(remaining) == 3

    def test_general_instruction_passthrough(self):
        """测试通用指令传递。"""
        state = self._create_sample_state()
        result = _process_complex_instruction("请仔细检查代码质量", state)
        
        # 通用指令应该保持原样传递
        assert result.get("user_instructions") == "请仔细检查代码质量"
        assert "pending_user_approval" in result
