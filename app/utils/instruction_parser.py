"""用户指令解析器 - 多轮审查智能指令处理。

解析用户的自然语言指令，提取结构化的操作意图。
支持的指令模式：
- 忽略特定类别问题
- 只关注特定类别问题
- 设置最大轮次
- 停止/继续执行
- 针对特定文件的指令
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 问题类别关键词映射
CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "bug": ["bug", "错误", "缺陷", "异常", "崩溃", "crash", "error"],
    "security": ["安全", "security", "漏洞", "风险", "注入", "xss", "csrf", "权限"],
    "performance": ["性能", "performance", "优化", "效率", "内存", "cpu", "缓存", "慢"],
    "style": ["风格", "style", "格式", "规范", "命名", "注释", "缩进"],
    "general": ["通用", "general", "其他", "all", "所有"],
}

# 操作意图关键词
ACTION_PATTERNS: Dict[str, List[str]] = {
    "ignore": ["忽略", "跳过", "无视", "ignore", "skip", "不要管"],
    "focus": ["只关注", "只修复", "重点", "focus", "only", "优先"],
    "stop": ["停止", "结束", "别修了", "stop", "end", "quit", "exit"],
    "continue": ["继续", "继续修", "再来", "continue", "go on"],
    "max_rounds": ["最多", "最多修", "限制", "上限", "max"],
    "all": ["所有", "全部", "所有问题", "全部问题", "all"],
}


class ParsedInstruction:
    """解析后的指令结构。"""
    
    def __init__(self):
        self.action: Optional[str] = None  # ignore, focus, stop, continue, max_rounds
        self.categories: List[str] = []  # 目标问题类别
        self.files: List[str] = []  # 目标文件
        self.max_rounds: Optional[int] = None  # 最大轮次
        self.raw_text: str = ""  # 原始文本
        self.confidence: float = 0.0  # 解析置信度
        
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典。"""
        return {
            "action": self.action,
            "categories": self.categories,
            "files": self.files,
            "max_rounds": self.max_rounds,
            "raw_text": self.raw_text,
            "confidence": self.confidence,
        }
        
    def __repr__(self) -> str:
        return f"ParsedInstruction(action={self.action}, categories={self.categories}, files={self.files})"


def parse_instruction(text: str) -> ParsedInstruction:
    """解析用户指令文本。
    
    Args:
        text: 用户输入的指令文本
        
    Returns:
        解析后的指令结构
    """
    result = ParsedInstruction()
    result.raw_text = text
    
    if not text or not text.strip():
        return result
    
    text_lower = text.lower().strip()
    
    # 1. 提取目标文件
    result.files = _extract_files(text)
    
    # 2. 提取目标类别
    result.categories = _extract_categories(text_lower)
    
    # 3. 识别操作意图
    action, confidence = _identify_action(text_lower)
    result.action = action
    result.confidence = confidence
    
    # 4. 提取轮次限制
    result.max_rounds = _extract_max_rounds(text_lower)
    
    logger.debug("解析指令: %s -> %s", text, result)
    return result


def _extract_files(text: str) -> List[str]:
    """从文本中提取文件路径。"""
    # 匹配文件路径模式
    file_patterns = [
        r'[\w/\\\.]+\.\w+',  # 标准文件路径
        r'文件[\s]*[：:]?[\s]*([\w/\\\.]+\.\w+)',  # 中文描述
        r'file[\s]*[：:]?[\s]*([\w/\\\.]+\.\w+)',  # 英文描述
    ]
    
    files = []
    for pattern in file_patterns:
        matches = re.findall(pattern, text)
        files.extend(matches)
    
    # 去重
    return list(set(files))


def _extract_categories(text_lower: str) -> List[str]:
    """从文本中提取问题类别。"""
    categories = []
    
    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in text_lower:
                categories.append(category)
                break
    
    # 如果没有匹配到，返回空列表
    return list(set(categories))


def _identify_action(text_lower: str) -> Tuple[Optional[str], float]:
    """识别操作意图。
    
    优先级规则：
    - 具体操作（ignore/focus）优先于通用修饰词（all）
    - 如果同时匹配到 ignore/focus 和 all，使用 ignore/focus
    
    Returns:
        (action, confidence) 元组
    """
    scores: Dict[str, float] = {}
    
    for action, keywords in ACTION_PATTERNS.items():
        score = 0
        for keyword in keywords:
            if keyword in text_lower:
                # 关键词越长，得分越高
                score += len(keyword) / len(text_lower)
        if score > 0:
            scores[action] = min(score * 2, 1.0)  # 归一化到 0-1
    
    if not scores:
        return None, 0.0
    
    # 优先级规则：具体操作优先于通用修饰词
    # 如果同时有 ignore/focus 和 all，优先使用 ignore/focus
    priority_actions = ["ignore", "focus"]
    for priority in priority_actions:
        if priority in scores and "all" in scores:
            # 返回具体操作，但使用 all 的分数（因为通常 all 匹配更具体）
            return priority, max(scores[priority], scores["all"])
    
    # 返回得分最高的操作
    best_action = max(scores, key=scores.get)
    return best_action, scores[best_action]


def _extract_max_rounds(text_lower: str) -> Optional[int]:
    """提取轮次限制。"""
    # 匹配模式：数字 + 次/轮/修复/重试
    patterns = [
        r'(\d+)\s*(?:次|轮|修复|重试|round)',
        r'(?:最多|上限|限制)\s*(\d+)',
        r'max[\s]*(\d+)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text_lower)
        if match:
            try:
                return int(match.group(1))
            except (ValueError, IndexError):
                continue
    
    return None


def categorize_instruction(instruction: ParsedInstruction) -> str:
    """将解析后的指令分类为标准操作类型。
    
    Returns:
        标准操作类型字符串
    """
    # 如果有明确的轮次限制
    if instruction.max_rounds is not None:
        return "set_max_rounds"
    
    # 如果有明确的操作意图
    if instruction.action == "stop":
        return "stop"
    elif instruction.action == "continue":
        return "continue"
    elif instruction.action == "ignore":
        if "所有" in instruction.raw_text or "全部" in instruction.raw_text:
            return "ignore_all"
        elif instruction.categories:
            return "ignore_categories"
        elif instruction.files:
            return "ignore_files"
        else:
            return "ignore_all"
    elif instruction.action == "all":
        # "所有"、"全部" 被识别为 all action
        # 根据上下文判断是忽略还是关注
        if "忽略" in instruction.raw_text or "无视" in instruction.raw_text:
            return "ignore_all"
        else:
            return "focus_all"
    elif instruction.action == "focus":
        if instruction.categories:
            return "focus_categories"
        elif instruction.files:
            return "focus_files"
        else:
            return "focus_all"
    
    # 如果只有类别信息，视为关注指令
    if instruction.categories:
        return "focus_categories"
    
    # 如果只有文件信息，视为针对文件的指令
    if instruction.files:
        return "file_specific"
    
    # 默认为通用指令
    return "general_instruction"


def format_instruction_summary(instruction: ParsedInstruction) -> str:
    """格式化指令摘要，用于日志或显示。"""
    parts = []
    
    if instruction.action:
        action_names = {
            "ignore": "忽略",
            "focus": "关注",
            "stop": "停止",
            "continue": "继续",
            "max_rounds": "设置轮次",
        }
        parts.append(f"操作: {action_names.get(instruction.action, instruction.action)}")
    
    if instruction.categories:
        category_names = {
            "bug": "Bug",
            "security": "安全",
            "performance": "性能",
            "style": "风格",
            "general": "通用",
        }
        names = [category_names.get(c, c) for c in instruction.categories]
        parts.append(f"类别: {', '.join(names)}")
    
    if instruction.files:
        parts.append(f"文件: {', '.join(instruction.files[:3])}")
        if len(instruction.files) > 3:
            parts.append(f"... 共 {len(instruction.files)} 个文件")
    
    if instruction.max_rounds is not None:
        parts.append(f"最大轮次: {instruction.max_rounds}")
    
    if instruction.confidence > 0:
        parts.append(f"置信度: {instruction.confidence:.0%}")
    
    return " | ".join(parts) if parts else "(无明确指令)"
