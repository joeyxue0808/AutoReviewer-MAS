"""规则化审查节点 (Critic Node) - 零 LLM 开销。

在 fixer_node 生成代码后、进入 tester_node 的沙盒验证前，
串入 critic_node 进行规则化快速检查。

检查策略：
- 严格拒绝：空 search、search 与 replace 完全相同
- 仅警告：括号不匹配（Vue/TypeScript 模板语法天然导致误报）
- 实际匹配验证由 PatchApplier 在写入时执行
"""

import logging
from typing import Any, Dict

from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)


async def critic_node(state: ReviewState) -> Dict[str, Any]:
    """Critic 节点函数 - 规则化快速检查（零 LLM 开销）。"""
    blocks = state.get("search_replace_blocks", [])

    if not blocks:
        logger.info("Critic: 无 block 需要审查")
        return {}

    logger.info("Critic 节点开始审查: %d 个 block", len(blocks))

    fatal_issues = []   # 导致拒绝的问题
    warnings = []       # 仅警告，不拒绝

    for i, block in enumerate(blocks):
        search = block.get("search_block", block.get("search", ""))
        replace = block.get("replace_block", block.get("replace", ""))
        fp = block.get("file_path", "?")

        # ── 严格拒绝 ──

        # search 为空或过短
        if not search or len(search.strip()) < 10:
            fatal_issues.append(f"Block #{i+1} ({fp}): search 内容过短或为空")

        # search 和 replace 完全相同
        if search.strip() == replace.strip():
            fatal_issues.append(f"Block #{i+1} ({fp}): search 和 replace 完全相同")

        # replace 为空但 search 不为空（误删代码）
        if search.strip() and not replace.strip():
            fatal_issues.append(f"Block #{i+1} ({fp}): replace 为空，将删除代码")

        # ── 仅警告（不拒绝）──

        # 括号检查（Vue/TypeScript 模板 {{ }}、字符串中的括号会导致误报）
        for ch, name in [("(", "圆括号"), ("[", "方括号"), ("{", "花括号")]:
            close = {"(": ")", "[": "]", "{": "}"}[ch]
            if search.count(ch) != search.count(close):
                warnings.append(f"Block #{i+1} ({fp}): search 中{name}数量不等（可能是模板语法）")
            if replace.count(ch) != replace.count(close):
                warnings.append(f"Block #{i+1} ({fp}): replace 中{name}数量不等")

    # 输出警告（不阻断流程）
    if warnings:
        for w in warnings:
            logger.warning("Critic 警告: %s", w)

    # 仅 fatal 问题才拒绝
    if fatal_issues:
        reason = "; ".join(fatal_issues)
        logger.warning("Critic 拒绝: %s", reason)
        return {
            "search_replace_blocks": [],
            "test_logs": f"Critic 拒绝: {reason}",
            "is_test_passed": False,
        }

    logger.info("Critic 通过: %d 个 block 合格 (%d 个警告)", len(blocks), len(warnings))
    return {}
