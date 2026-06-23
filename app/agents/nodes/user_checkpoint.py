"""用户检查点节点 - 多轮审查交互支持。

等待并处理用户输入，决定审查流程的下一步。

设计说明：
- 使用 LangGraph 的 interrupt() 机制暂停图执行
- CLI 层通过 graph.update_state() 恢复执行
- Webhook 模式通过 Webhook 回调恢复执行
"""

import logging
from typing import Any, Dict, Optional

from langgraph.types import interrupt

from app.schemas.state import ReviewState
from app.schemas.user_input import UserActionType, UserInput, parse_user_input
from app.utils.instruction_parser import (
    ParsedInstruction,
    categorize_instruction,
    format_instruction_summary,
    parse_instruction,
)

logger = logging.getLogger(__name__)


def user_checkpoint_node(state: ReviewState) -> Dict[str, Any]:
    """用户检查点节点 - 使用 interrupt 机制等待用户输入。

    工作流程：
    1. 首次进入时，生成问题摘要
    2. 调用 interrupt() 暂停图执行，等待外部输入
    3. 外部输入通过 graph.update_state() 注入后恢复执行
    4. 解析用户输入，更新状态

    Returns:
        状态更新字典
    """
    current_round = state.get("current_round", 0)
    max_rounds = state.get("max_rounds", 3)
    review_issues = state.get("review_issues", [])
    remaining_issues = state.get("remaining_issues", [])
    
    logger.info(
        "用户检查点: 第 %d/%d 轮, 剩余问题 %d",
        current_round + 1,
        max_rounds,
        len(remaining_issues),
    )
    
    # 生成问题摘要供用户决策
    issue_summary = _generate_issue_summary(remaining_issues or review_issues)
    
    # 如果配置为自动批准，跳过用户交互
    if state.get("auto_approve", False):
        logger.info("自动批准模式: 继续流程")
        return {
            "pending_user_approval": False,
            "user_approval_result": True,
            "remaining_issues": remaining_issues or review_issues,
        }
    
    # 使用 interrupt 暂停图执行，等待用户输入
    # interrupt 的值会被传递给外部，外部通过 update_state 注入用户输入
    logger.info("暂停执行，等待用户输入...")
    user_response = interrupt({
        "type": "user_approval",
        "round": current_round + 1,
        "max_rounds": max_rounds,
        "issue_summary": issue_summary,
        "issue_count": len(remaining_issues or review_issues),
        "prompt": f"是否修复以上 {len(remaining_issues or review_issues)} 个问题？(y/n/指令)",
    })
    
    # 恢复执行后，处理用户输入
    # user_response 是通过 update_state 注入的值
    if isinstance(user_response, str):
        parsed_input = parse_user_input(user_response)
    elif isinstance(user_response, UserInput):
        parsed_input = user_response
    elif isinstance(user_response, dict) and "action" in user_response:
        try:
            parsed_input = UserInput.model_validate(user_response)
        except Exception as e:
            logger.warning("用户输入验证失败: %s", e)
            parsed_input = UserInput(action=UserActionType.APPROVE, timestamp=0)
    else:
        logger.warning("无法识别的用户输入: %s，使用默认批准", user_response)
        parsed_input = UserInput(action=UserActionType.APPROVE, timestamp=0)
    
    # 根据用户操作处理
    return _process_user_input(parsed_input, state)


def _generate_issue_summary(issues: list) -> str:
    """生成问题摘要文本。"""
    if not issues:
        return "无问题"
    
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    category_counts = {}
    
    for issue in issues:
        if isinstance(issue, dict):
            severity = issue.get("severity", "info")
            category = issue.get("category", "general")
        else:
            severity = getattr(issue, "severity", "info")
            category = getattr(issue, "category", "general")
        
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1
    
    parts = []
    if severity_counts["critical"]:
        parts.append(f"🔴 Critical: {severity_counts['critical']}")
    if severity_counts["warning"]:
        parts.append(f"🟡 Warning: {severity_counts['warning']}")
    if severity_counts["info"]:
        parts.append(f"🔵 Info: {severity_counts['info']}")
    
    return " | ".join(parts)


def _process_user_input(user_input: UserInput, state: ReviewState) -> Dict[str, Any]:
    """处理用户输入，返回状态更新。"""
    action = user_input.action
    content = user_input.content or ""
    review_issues = state.get("review_issues", [])
    remaining_issues = state.get("remaining_issues", [])
    effective_issues = remaining_issues or review_issues
    
    # 批准当前修复
    if action == UserActionType.APPROVE:
        logger.info("用户批准修复")
        return {
            "user_approval_result": True,
            "pending_user_approval": False,
            "user_instructions": content,
            "remaining_issues": effective_issues,
        }
    
    # 拒绝当前修复
    if action == UserActionType.REJECT:
        logger.info("用户拒绝修复")
        return {
            "user_approval_result": False,
            "pending_user_approval": False,
            "user_instructions": content,
            "remaining_issues": [],
        }
    
    # 提供新指令 — 使用 instruction_parser 智能解析
    if action == UserActionType.INSTRUCTION:
        logger.info("用户指令: %s", content)
        return _process_complex_instruction(content, state)
    
    # 停止执行
    if action == UserActionType.STOP:
        logger.info("用户停止执行")
        return {
            "user_approval_result": False,
            "pending_user_approval": False,
            "remaining_issues": [],  # 清空剩余问题
            "current_round": state.get("max_rounds", 3),  # 强制达到最大轮次
        }
    
    # 跳过当前轮次
    if action == UserActionType.SKIP_ROUND:
        logger.info("用户跳过当前轮次")
        return {
            "current_round": state.get("current_round", 0) + 1,
            "pending_user_approval": False,
        }
    
    # 忽略特定问题
    if action == UserActionType.IGNORE_ISSUES:
        # 解析要忽略的问题类别
        ignore_categories = _extract_categories(content)
        logger.info("忽略问题类别: %s", ignore_categories)
        
        # 过滤掉要忽略的问题
        remaining_issues = [
            issue for issue in state.get("remaining_issues", [])
            if issue.get("category", "general") not in ignore_categories
        ]
        
        return {
            "remaining_issues": remaining_issues,
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 只关注特定问题
    if action == UserActionType.FOCUS_ISSUES:
        # 解析要关注的问题类别
        focus_categories = _extract_categories(content)
        logger.info("关注问题类别: %s", focus_categories)
        
        # 只保留要关注的问题
        remaining_issues = [
            issue for issue in state.get("remaining_issues", [])
            if issue.get("category", "general") in focus_categories
        ]
        
        return {
            "remaining_issues": remaining_issues,
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 默认继续流程
    return {"pending_user_approval": False}


def _process_complex_instruction(content: str, state: ReviewState) -> Dict[str, Any]:
    """使用 instruction_parser 智能解析复杂指令。

    支持的指令类型：
    - 忽略特定类别："忽略性能问题"
    - 只关注特定类别："只关注安全问题"
    - 停止执行："停止" "3次修复后停止"
    - 继续执行："继续修复"
    - 设置轮次："最多修2次"
    - 针对特定文件："检查 a.py 的安全性"
    """
    parsed = parse_instruction(content)
    instruction_type = categorize_instruction(parsed)
    summary = format_instruction_summary(parsed)
    
    logger.info(
        "指令解析: type=%s, categories=%s, files=%s, max_rounds=%s, confidence=%.0f%%",
        instruction_type,
        parsed.categories,
        parsed.files,
        parsed.max_rounds,
        parsed.confidence * 100,
    )
    logger.info("指令摘要: %s", summary)
    
    effective_issues = state.get("remaining_issues", []) or state.get("review_issues", [])
    
    # 1. 停止指令
    if instruction_type == "stop":
        logger.info("指令解析: 停止执行")
        return {
            "user_approval_result": False,
            "pending_user_approval": False,
            "remaining_issues": [],
            "user_instructions": content,
            "current_round": state.get("max_rounds", 3),
        }
    
    # 2. 继续指令
    if instruction_type == "continue":
        logger.info("指令解析: 继续修复")
        return {
            "user_approval_result": True,
            "pending_user_approval": False,
            "remaining_issues": effective_issues,
            "user_instructions": content,
        }
    
    # 3. 忽略特定类别
    if instruction_type == "ignore_categories" and parsed.categories:
        logger.info("指令解析: 忽略类别 %s", parsed.categories)
        filtered_issues = [
            issue for issue in effective_issues
            if issue.get("category", "general") not in parsed.categories
        ]
        return {
            "remaining_issues": filtered_issues,
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 4. 只关注特定类别
    if instruction_type == "focus_categories" and parsed.categories:
        logger.info("指令解析: 关注类别 %s", parsed.categories)
        filtered_issues = [
            issue for issue in effective_issues
            if issue.get("category", "general") in parsed.categories
        ]
        return {
            "remaining_issues": filtered_issues,
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 5. 针对特定文件
    if instruction_type == "file_specific" and parsed.files:
        logger.info("指令解析: 关注文件 %s", parsed.files)
        filtered_issues = [
            issue for issue in effective_issues
            if issue.get("file_path", "") in parsed.files
        ]
        return {
            "remaining_issues": filtered_issues,
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 6. 设置最大轮次
    if instruction_type == "set_max_rounds" and parsed.max_rounds is not None:
        logger.info("指令解析: 设置最大轮次 %d", parsed.max_rounds)
        return {
            "max_rounds": parsed.max_rounds,
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 7. 忽略全部
    if instruction_type == "ignore_all":
        logger.info("指令解析: 忽略全部问题")
        return {
            "remaining_issues": [],
            "user_instructions": content,
            "pending_user_approval": False,
        }
    
    # 8. 未识别的指令 — 作为通用指令传递
    logger.info("指令解析: 通用指令 (未匹配特定模式，置信度 %.0f%%)", parsed.confidence * 100)
    return {
        "user_instructions": content,
        "pending_user_approval": False,
    }


def _extract_categories(text: str) -> list:
    """从文本中提取问题类别。"""
    import re
    
    # 定义问题类别关键词映射
    category_keywords = {
        "bug": ["bug", "错误", "缺陷", "异常"],
        "security": ["安全", "security", "漏洞", "风险"],
        "performance": ["性能", "performance", "优化", "效率"],
        "style": ["风格", "style", "格式", "规范"],
        "general": ["通用", "general", "其他"],
    }
    
    categories = []
    text_lower = text.lower()
    
    for category, keywords in category_keywords.items():
        # 跳过 "general" 类别，避免通用关键词干扰
        if category == "general":
            continue
        for keyword in keywords:
            if keyword in text_lower:
                categories.append(category)
                break
    
    # 如果没有匹配到任何特定类别，返回默认类别 "general"
    if not categories:
        categories = ["general"]
    
    return list(set(categories))


def after_user_checkpoint(state: ReviewState) -> str:
    """用户检查点后的路由逻辑。

    Returns:
        下一个节点名称
    """
    user_approval_result = state.get("user_approval_result")
    pending_user_approval = state.get("pending_user_approval", False)
    remaining_issues = state.get("remaining_issues", [])
    current_round = state.get("current_round", 0)
    max_rounds = state.get("max_rounds", 3)
    
    # 如果还在等待用户批准
    if pending_user_approval:
        return "user_checkpoint_node"  # 继续等待
    
    # 如果用户拒绝修复
    if user_approval_result is False:
        # 检查是否还有剩余问题
        if remaining_issues:
            return "reviewer_node"  # 重新审查
        else:
            return "submit_node"  # 直接提交
    
    # 如果用户批准修复
    if user_approval_result is True:
        # 检查是否还有剩余问题
        if remaining_issues:
            return "fixer_node"  # 进行修复
        else:
            return "submit_node"  # 直接提交
    
    # 如果用户提供了指令但没有明确批准/拒绝
    user_instructions = state.get("user_instructions", "")
    
    # 检查是否应该停止
    if "停止" in user_instructions or "stop" in user_instructions.lower():
        return "submit_node"
    
    # 检查是否跳过当前轮次
    if "跳过" in user_instructions or "skip" in user_instructions.lower():
        # 注意：current_round 在 decision_node 中递增，这里检查的是当前状态
        if current_round >= max_rounds:
            return "submit_node"
        else:
            return "reviewer_node"  # 重新审查（轮次递增将在 decision_node 中处理）
    
    # 默认继续修复
    if remaining_issues:
        return "fixer_node"
    else:
        return "submit_node"
