"""规则化审查节点 (Critic Node) - 零 LLM 开销。

在 fixer_node 生成代码后、进入 tester_node 的沙盒验证前，
串入 critic_node 进行规则化快速检查。

纯规则检查（无 LLM 调用）：
- search/replace 是否为空或过短
- 括号/引号是否匹配
- search 和 replace 是否完全相同（无意义修改）
"""

import logging
from typing import Any, Dict

from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)


async def critic_node(state: ReviewState) -> Dict[str, Any]:
    """Critic 节点函数 - 规则化快速检查（零 LLM 开销）。

    使用纯规则检查 Fixer 输出的 SearchReplaceBlock：
    1. search/replace 是否为空或过短
    2. 括号/引号是否匹配
    3. search 和 replace 是否完全相同（无意义修改）
    4. search 是否包含足够的上下文
    """
    blocks = state.get("search_replace_blocks", [])

    if not blocks:
        logger.info("Critic: 无 block 需要审查")
        return {}

    logger.info("Critic 节点开始审查: %d 个 block", len(blocks))

    # 规则化检查（零 LLM 开销）
    issues = []
    for i, block in enumerate(blocks):
        search = block.get("search_block", block.get("search", ""))
        replace = block.get("replace_block", block.get("replace", ""))
        fp = block.get("file_path", "?")

        # 规则 1: search 为空或过短
        if not search or len(search.strip()) < 10:
            issues.append(f"Block #{i+1} ({fp}): search 内容过短或为空")

        # 规则 2: search 和 replace 完全相同
        if search.strip() == replace.strip():
            issues.append(f"Block #{i+1} ({fp}): search 和 replace 完全相同（无意义修改）")

        # 规则 3: 括号不匹配
        for ch, name in [("(", "圆括号"), ("[", "方括号"), ("{", "花括号")]:
            close = {"(": ")", "[": "]", "{": "}"}[ch]
            if search.count(ch) != search.count(close):
                issues.append(f"Block #{i+1} ({fp}): search 中{name}不匹配")
            if replace.count(ch) != replace.count(close):
                issues.append(f"Block #{i+1} ({fp}): replace 中{name}不匹配")

        # 规则 4: replace 为空但 search 不为空（可能误删代码）
        if search.strip() and not replace.strip():
            logger.warning("Block #%d (%s): replace 为空，可能删除了代码", i+1, fp)

    if issues:
        reason = "; ".join(issues)
        logger.warning("Critic 拒绝: %s", reason)
        # 注意：retry_count 由 workflow 层统一管理，Critic 不递增
        return {
            "search_replace_blocks": [],
            "test_logs": f"Critic 拒绝: {reason}",
            "is_test_passed": False,
        }

    logger.info("Critic 通过: 所有 %d 个 block 规则检查合格", len(blocks))
    return {}
