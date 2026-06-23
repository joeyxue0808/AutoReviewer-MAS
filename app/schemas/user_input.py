"""用户输入事件模型 - 多轮审查交互支持。

定义用户在审查过程中可以执行的操作类型和输入格式。
"""

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class UserActionType(str, Enum):
    """用户操作类型枚举。"""
    
    APPROVE = "approve"          # 批准当前修复
    REJECT = "reject"            # 拒绝当前修复
    INSTRUCTION = "instruction"  # 提供新指令
    STOP = "stop"                # 停止执行
    SKIP_ROUND = "skip_round"    # 跳过当前轮次
    IGNORE_ISSUES = "ignore_issues"  # 忽略特定问题
    FOCUS_ISSUES = "focus_issues"    # 只关注特定问题


class UserInput(BaseModel):
    """用户输入事件模型。"""
    
    action: UserActionType = Field(description="用户操作类型")
    content: Optional[str] = Field(default=None, description="指令内容")
    target_issues: Optional[List[str]] = Field(default=None, description="针对特定问题的指令")
    timestamp: float = Field(description="输入时间戳")
    
    class Config:
        """Pydantic 配置。"""
        use_enum_values = True


class UserDecision(BaseModel):
    """用户决策结果。"""
    
    approved: bool = Field(description="是否批准")
    instructions: Optional[str] = Field(default=None, description="附带指令")
    ignore_categories: List[str] = Field(default_factory=list, description="要忽略的问题类别")
    focus_categories: List[str] = Field(default_factory=list, description="要关注的问题类别")
    max_rounds_override: Optional[int] = Field(default=None, description="覆盖最大轮次设置")


def parse_user_input(raw_input: str) -> UserInput:
    """解析用户原始输入为结构化 UserInput。
    
    Args:
        raw_input: 用户输入的原始字符串
        
    Returns:
        解析后的 UserInput 对象
    """
    import time
    
    raw_lower = raw_input.strip().lower()
    
    # 批准类指令
    if raw_lower in ("y", "yes", "是", "确认", "approve", "批准"):
        return UserInput(action=UserActionType.APPROVE, timestamp=time.time())
    
    # 拒绝类指令
    if raw_lower in ("n", "no", "否", "拒绝", "reject"):
        return UserInput(action=UserActionType.REJECT, timestamp=time.time())
    
    # 停止指令
    if raw_lower in ("stop", "停止", "exit", "退出", "quit"):
        return UserInput(action=UserActionType.STOP, timestamp=time.time())
    
    # 跳过当前轮次
    if raw_lower in ("skip", "跳过", "next", "下一轮"):
        return UserInput(action=UserActionType.SKIP_ROUND, timestamp=time.time())
    
    # 忽略特定问题
    if raw_lower.startswith(("忽略", "ignore")):
        return UserInput(
            action=UserActionType.IGNORE_ISSUES,
            content=raw_input.strip(),
            timestamp=time.time(),
        )
    
    # 只关注特定问题
    if raw_lower.startswith(("只关注", "只修复", "focus", "only fix")):
        return UserInput(
            action=UserActionType.FOCUS_ISSUES,
            content=raw_input.strip(),
            timestamp=time.time(),
        )
    
    # 其他输入视为指令
    return UserInput(
        action=UserActionType.INSTRUCTION,
        content=raw_input.strip(),
        timestamp=time.time(),
    )
