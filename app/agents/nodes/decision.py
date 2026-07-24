"""决策节点 - 多轮审查循环控制。

负责检查是否应该继续下一轮审查，基于以下条件：
1. 当前轮次是否达到最大限制
2. 是否还有剩余问题需要处理
3. 用户是否提供了停止指令
4. 是否检测到收敛（连续无新问题）
"""

import logging
import re
import time
from typing import Any, Dict, List

from app.schemas.state import ReviewState
from app.core.config import settings

logger = logging.getLogger(__name__)

# 收敛阈值 - 从配置加载，连续 N 轮零问题即收敛
_convergence_threshold: int = settings.multiround.convergence_threshold


def decision_node(state: ReviewState) -> Dict[str, Any]:
    """决策节点 - 更新轮次状态并记录报告。

    职责：
    1. 递增当前轮次
    2. 记录本轮报告
    3. 更新剩余问题列表

    Returns:
        状态更新字典
    """
    current_round = state.get("current_round", 0)
    remaining_issues = state.get("remaining_issues", [])
    fixed_issues = state.get("fixed_issues", [])
    round_reports = state.get("round_reports", [])
    
    # 记录本轮报告
    round_report = {
        "round": current_round,
        "issues_count": len(remaining_issues),
        "fixed_count": len(fixed_issues),
        "timestamp": time.time(),
    }
    
    logger.info(
        "决策节点: 第 %d 轮完成, 剩余问题 %d, 已修复问题 %d",
        current_round + 1,
        len(remaining_issues),
        len(fixed_issues),
    )
    
    return {
        "current_round": current_round + 1,
        "round_reports": round_reports + [round_report],
    }


def make_decision(state: ReviewState) -> str:
    """决策路由 - 确定下一步操作。

    检查终止条件：
    1. 达到最大轮次限制
    2. 没有剩余问题
    3. 用户停止指令
    4. 连续收敛（可选）

    Returns:
        下一个节点名称
    """
    current_round = state.get("current_round", 0)
    max_rounds = state.get("max_rounds", settings.max_retry_count)
    remaining_issues = state.get("remaining_issues", [])
    user_instructions = state.get("user_instructions", "")
    fixed_issues = state.get("fixed_issues", [])
    
    logger.info(
        "决策路由: 第 %d/%d 轮, 剩余问题 %d, 已修复问题 %d",
        current_round,
        max_rounds,
        len(remaining_issues),
        len(fixed_issues),
    )
    
    # 1. 检查用户停止指令
    stop_result = _check_user_stop_command(user_instructions, current_round)
    if stop_result == "stop":
        logger.info("用户指令停止，结束流程")
        return "submit_node"
    
    # 2. 检查是否达到最大轮次
    if current_round >= max_rounds:
        logger.warning("达到最大轮次 (%d/%d)，强制结束", current_round, max_rounds)
        return "submit_node"
    
    # 3. 检查是否还有剩余问题
    if not remaining_issues:
        logger.info("没有剩余问题，流程结束")
        return "submit_node"
    
    # 4. 检查收敛条件（可选）
    if _check_convergence(state):
        logger.info("检测到收敛，流程结束")
        return "submit_node"
    
    # 5. 继续下一轮审查
    logger.info("继续下一轮审查 (%d/%d)", current_round + 1, max_rounds)
    return "reviewer_node"


def _check_user_stop_command(instructions: str, current_round: int) -> str:
    """检查用户指令是否包含停止命令。

    Args:
        instructions: 用户指令文本
        current_round: 当前轮次

    Returns:
        "stop" 表示停止，"continue" 表示继续
    """
    if not instructions:
        return "continue"
    
    instructions_lower = instructions.lower()
    
    # 检查直接停止关键词
    stop_keywords = ["停止", "stop", "exit", "退出", "quit", "结束", "end"]
    for keyword in stop_keywords:
        if keyword in instructions_lower:
            return "stop"
    
    # 检查轮次限制模式，如 "第3次修复后停止"，使用更精确的匹配
    stop_pattern = r"(\d+)\s*(次|轮|修复|重试)\s*后?\s*停止"
    matches = re.findall(stop_pattern, instructions_lower)
    if matches:
        # 取最后一个匹配的数字作为目标轮次
        target_round = int(matches[-1][0])
        if current_round >= target_round:
            logger.info("用户指令: 达到第 %d 轮后停止", target_round)
            return "stop"
    
    return "continue"


def _check_convergence(state: ReviewState) -> bool:
    """检查是否收敛（连续无新问题）。

    使用配置中的 convergence_threshold（默认 2），
    即连续 N 轮问题数为 0 时认为收敛。
    """
    round_reports = state.get("round_reports", [])
    threshold = _convergence_threshold

    if len(round_reports) < threshold:
        return False

    # 检查最近 N 轮的问题数是否都为 0
    last_n = round_reports[-threshold:]
    if all(r.get("issues_count", 0) == 0 for r in last_n):
        logger.info("收敛检测: 连续 %d 轮零问题，判定收敛", threshold)
        return True

    return False


def after_critic(state: ReviewState) -> str:
    """批评节点后的路由逻辑（用于多轮循环）。

    根据批评节点的结果，决定下一步：
    1. 如果有 critical 问题，回到 fixer_node
    2. 如果没有问题，进入决策节点
    """
    _issues = state.get("deduplicated_issues", state.get("review_issues", []))
    critical_issues = [
        issue for issue in _issues
        if (issue.get("severity") if isinstance(issue, dict) else getattr(issue, "severity", "")) == "critical"
    ]
    
    if critical_issues:
        logger.info("批评节点发现 %d 个 critical 问题，回到修复节点", len(critical_issues))
        return "fixer_node"
    else:
        logger.info("批评节点通过，进入决策节点")
        return "decision_node"


def after_reviewer_multiround(state: ReviewState) -> str:
    """审查节点后的路由逻辑（多轮版本）。"""
    # 检查错误恢复
    if state.get("error_type"):
        logger.info("审查节点出错 (%s)", state["error_type"])
        return "error_recovery_node"
    
    # 使用去重后的问题列表
    _issues = state.get("deduplicated_issues", state.get("review_issues", []))
    critical_issues = [
        issue for issue in _issues
        if (issue.get("severity") if isinstance(issue, dict) else getattr(issue, "severity", "")) == "critical"
    ]
    
    # 如果有 critical 问题，直接进入修复节点
    if critical_issues:
        logger.info("发现 %d 个 critical 问题，进入修复节点", len(critical_issues))
        return "fixer_node"
    
    # 如果没有 critical 问题，进入用户检查点让用户决定
    logger.info("没有 critical 问题，进入用户检查点")
    return "user_checkpoint_node"
