"""红蓝对抗审查节点 (Critic Node) - Phase 3 资源优化。

在 fixer_node 生成代码后、进入 tester_node 的沙盒验证前，
串入 critic_node 进行快速挑刺。

使用低延迟小模型审查生成的 SearchReplaceBlock：
- 若发现明显的语法截断 → 打回 Fixer 重试
- 若发现变量未声明 → 打回 Fixer 重试
- 若发现 search 字符串明显不匹配 → 打回 Fixer 重试

优势：大幅节省沙盒（Docker）冷启动与编译的昂贵开销。
"""

import logging
from typing import Any, Dict, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm_factory import get_llm
from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)

CRITIC_SYSTEM_PROMPT = """你是一位代码审查 Critic，专门负责快速验证 Fixer 生成的 Search/Replace Block 的质量。

你的检查项：
1. search 字符串是否包含足够的上下文（至少 3 行有意义的代码）
2. replace 字符串是否语法完整（没有截断、括号不匹配等明显问题）
3. search 和 replace 中的变量名是否一致（不会引入未声明的变量）
4. 修改是否过于激进（如删除了整个函数体）

判断规则：
- 如果发现任何严重问题，返回 "reject" 并说明原因
- 如果所有 block 看起来合理，返回 "pass"

只输出 JSON 格式：{"verdict": "pass" 或 "reject", "reason": "原因说明"}"""


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
        retry_count = state.get("retry_count", 0)
        return {
            "search_replace_blocks": [],
            "test_logs": f"Critic 拒绝: {reason}",
            "is_test_passed": False,
            "retry_count": retry_count + 1,
        }

    logger.info("Critic 通过: 所有 %d 个 block 规则检查合格", len(blocks))
    return {}


def _format_blocks(blocks: list) -> str:
    """将 SearchReplaceBlock 格式化为可读文本。"""
    lines = []
    for i, block in enumerate(blocks, 1):
        fp = block.get("file_path", "?")
        search = block.get("search_block", block.get("search", ""))[:200]
        replace = block.get("replace_block", block.get("replace", ""))[:200]
        lines.append(
            f"Block #{i}: {fp}\n"
            f"  search ({len(block.get('search', ''))} chars): {search!r}...\n"
            f"  replace ({len(block.get('replace', ''))} chars): {replace!r}..."
        )
    return "\n\n".join(lines)


def _parse_verdict(content: str) -> Dict[str, str]:
    """解析 Critic 的 JSON 输出。"""
    import json

    # 尝试从内容中提取 JSON
    try:
        # 处理 markdown code block 包裹的情况
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 降级：关键词匹配
        content_lower = content.lower()
        if "reject" in content_lower:
            return {"verdict": "reject", "reason": content[:200]}
        return {"verdict": "pass", "reason": "Critic 输出非 JSON，默认放行"}
