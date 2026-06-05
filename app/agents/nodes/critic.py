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
    """Critic 节点函数。

    快速审查 Fixer 输出的 SearchReplaceBlock。
    如果发现问题，清空 blocks 并递增 retry_count，强制 Fixer 重新生成。
    """
    blocks = state.get("search_replace_blocks", [])

    if not blocks:
        logger.info("Critic: 无 block 需要审查")
        return {}

    logger.info("Critic 节点开始审查: %d 个 block", len(blocks))

    # 格式化 blocks 为可读文本
    blocks_text = _format_blocks(blocks)

    # 使用低延迟小模型（tester 角色，temperature=0.1）
    llm = get_llm("tester")

    messages = [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=f"请审查以下 Search/Replace Block：\n\n{blocks_text}"),
    ]

    try:
        response = await llm.ainvoke(messages)
        content = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        logger.error("Critic LLM 调用失败: %s，放行", e)
        return {}

    # 解析 verdict
    verdict = _parse_verdict(content)

    if verdict.get("verdict") == "reject":
        reason = verdict.get("reason", "未知原因")
        logger.warning("Critic 拒绝: %s", reason)

        # 清空 blocks，递增 retry_count，强制 Fixer 重试
        retry_count = state.get("retry_count", 0)
        return {
            "search_replace_blocks": [],
            "test_logs": f"Critic 拒绝: {reason}",
            "is_test_passed": False,
            "retry_count": retry_count + 1,
        }

    logger.info("Critic 通过: %s", verdict.get("reason", "所有 block 质量合格"))
    return {}


def _format_blocks(blocks: list) -> str:
    """将 SearchReplaceBlock 格式化为可读文本。"""
    lines = []
    for i, block in enumerate(blocks, 1):
        fp = block.get("file_path", "?")
        search = block.get("search", "")[:200]
        replace = block.get("replace", "")[:200]
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
