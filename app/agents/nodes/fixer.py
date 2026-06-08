"""Fixer Agent 节点 - 极速模式。

核心优化：预加载审查问题涉及的文件上下文，注入 prompt，
Fixer 单次 LLM 调用生成 Search/Replace Block，无需 ReAct 工具调用。
"""

import logging
import re
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.prompts.fixer import FIXER_HUMAN_PROMPT, FIXER_SYSTEM_PROMPT
from app.core.llm_factory import get_llm
from app.schemas.llm_out import FixerOutput
from app.schemas.state import ReviewState
from app.tools import read_file_context

logger = logging.getLogger(__name__)

_CONTEXT_LINES = 80


async def fixer_node(state: ReviewState) -> Dict[str, Any]:
    """Fixer Agent 节点函数 — 极速模式。

    1. 从 review_issues 提取涉及的文件路径
    2. 预加载这些文件的上下文
    3. 一次性注入 prompt，单次 LLM 调用生成 Search/Replace Block
    """
    logger.info(
        "Fixer 节点开始执行: pr=%s, retry_count=%d",
        state.get("pr_id", "?"),
        state["retry_count"],
    )

    issues = state.get("review_issues", [])
    detected = state.get("detected_languages", [])
    issues_text = _format_issues(issues)

    # ── 预加载审查问题涉及的文件上下文 ──
    file_contexts = await _preload_issue_file_contexts(issues)
    logger.info("预加载 %d 个文件上下文", len(file_contexts))

    # 构建文件上下文文本
    context_text = ""
    for fp, content in file_contexts.items():
        context_text += f"\n### 文件: {fp}\n```python\n{content}\n```\n"

    # 构建 diff 文本
    diff_chunks = state.get("diff_chunks", {})
    diff_text = ""
    for lang, chunk in diff_chunks.items():
        diff_text += f"\n### [{lang}]\n```diff\n{chunk}\n```\n"

    system_msg = SystemMessage(content=FIXER_SYSTEM_PROMPT)
    human_msg = HumanMessage(
        content=FIXER_HUMAN_PROMPT.format(
            languages=", ".join(detected) if detected else "unknown",
            review_issues=issues_text,
            test_logs=state.get("test_logs", "（首次修复，无测试日志）"),
        )
        + "\n\n## 变更文件的完整上下文（已预加载，请直接生成修复，无需调用工具）\n"
        + context_text
        + "\n\n## 原始 Diff\n"
        + diff_text
    )

    # ── 单次 LLM 调用，结构化输出 ──
    trace_id = f"{state.get('vcs_provider', 'cli')}-{state.get('pr_id', 'local')}"
    llm = get_llm("fixer", trace_id=trace_id)
    structured_llm = llm.with_structured_output(FixerOutput, method="json_mode")

    try:
        output: FixerOutput = await structured_llm.ainvoke([system_msg, human_msg])
    except Exception as e:
        if "429" in str(e):
            logger.warning("Fixer 触发 rate limit (429)，保留已有结果")
        else:
            logger.error("Fixer LLM 调用失败: %s", e)
        return {
            "search_replace_blocks": state.get("search_replace_blocks", []),
            "retry_count": state["retry_count"],
            "error_count": state.get("error_count", 0) + 1,
        }

    logger.info(
        "Fixer 完成: %d 个 Search/Replace Block, explanation=%s",
        len(output.blocks),
        output.explanation[:100] if output.explanation else "",
    )

    blocks: List[Dict[str, Any]] = [
        {
            "file_path": block.file_path,
            "search_block": block.search,
            "replace_block": block.replace,
            "context_before": block.context_before,
            "context_after": block.context_after,
        }
        for block in output.blocks
    ]

    return {
        "search_replace_blocks": blocks,
        "retry_count": state["retry_count"] + 1,
    }


async def _preload_issue_file_contexts(issues: list) -> Dict[str, str]:
    """从审查问题中提取文件路径，并发读取上下文。"""
    import asyncio

    # 提取所有涉及的文件路径
    file_paths = set()
    for issue in issues:
        fp = issue.get("file_path", "") if isinstance(issue, dict) else getattr(issue, "file_path", "")
        ln = issue.get("line_number", 0) if isinstance(issue, dict) else getattr(issue, "line_number", 0)
        if fp:
            file_paths.add((fp, ln))

    if not file_paths:
        return {}

    async def read_one(fp: str, ln: int) -> tuple[str, str]:
        try:
            if ln:
                start = max(1, ln - _CONTEXT_LINES // 2)
                end = ln + _CONTEXT_LINES // 2
                content = await read_file_context.ainvoke({
                    "file_path": fp, "start_line": start, "end_line": end,
                })
            else:
                content = await read_file_context.ainvoke({"file_path": fp})
        except Exception:
            content = f"(无法读取 {fp})"
        content = str(content)
        if len(content) > 4000:
            content = content[:4000] + "\n... [截断]"
        return fp, content

    tasks = [read_one(fp, ln) for fp, ln in file_paths]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    contexts = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        fp, content = r
        contexts[fp] = content

    return contexts


def _format_issues(issues: list) -> str:
    """将审查问题列表格式化为可读文本。"""
    if not issues:
        return "（无审查问题）"

    lines = []
    for i, issue in enumerate(issues, 1):
        severity = issue.get("severity", "info") if isinstance(issue, dict) else getattr(issue, "severity", "info")
        file_path = issue.get("file_path", "") if isinstance(issue, dict) else getattr(issue, "file_path", "")
        line_number = issue.get("line_number", 0) if isinstance(issue, dict) else getattr(issue, "line_number", 0)
        description = issue.get("description", "") if isinstance(issue, dict) else getattr(issue, "description", "")
        suggestion = issue.get("suggestion", "") if isinstance(issue, dict) else getattr(issue, "suggestion", "")

        lines.append(
            f"{i}. [{severity.upper()}] {file_path}:{line_number}\n"
            f"   问题: {description}\n"
            f"   建议: {suggestion}"
        )
    return "\n".join(lines)
