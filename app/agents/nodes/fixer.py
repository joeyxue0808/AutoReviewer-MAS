"""Fixer Agent 节点 - Phase 2 Tool Calling。

V2 核心变更：放弃 Unified Diff，改为 Search/Replace Block。
Phase 2 变更：
- 绑定工具，Fixer 可调用 read_file_context 验证源文件内容
- 确保 search 字符串与源文件精确匹配
"""

import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.prompts.fixer import FIXER_HUMAN_PROMPT, FIXER_SYSTEM_PROMPT
from app.core.llm_factory import get_llm
from app.schemas.llm_out import FixerOutput
from app.schemas.state import ReviewState
from app.tools import read_file_context, list_directory

logger = logging.getLogger(__name__)

# Fixer 可用工具（主要是读取文件验证 search 内容）
TOOLS = [read_file_context, list_directory]
TOOL_MAP = {t.name: t for t in TOOLS}

MAX_TOOL_ITERATIONS = 3


async def fixer_node(state: ReviewState) -> Dict[str, Any]:
    """Fixer Agent 节点函数。

    Phase 2: Fixer 可调用 read_file_context 验证 search 内容精确性。
    """
    logger.info(
        "Fixer 节点开始执行: pr=%s, retry_count=%d",
        state.get("pr_id", "?"),
        state["retry_count"],
    )

    trace_id = f"{state.get('vcs_provider', 'cli')}-{state.get('pr_id', 'local')}"
    llm = get_llm("fixer", trace_id=trace_id)
    llm_with_tools = llm.bind_tools(TOOLS)

    detected = state.get("detected_languages", [])
    issues_text = _format_issues(state.get("review_issues", []))

    # 构建 diff_chunks 可读文本
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
    )
    messages: list = [system_msg, human_msg]

    # ─────────────────────────────────────────
    # ReAct 推理循环
    # ─────────────────────────────────────────
    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response: AIMessage = await llm_with_tools.ainvoke(messages)
        except Exception as e:
            logger.error("Fixer LLM 调用失败 (iteration %d): %s", iteration, e)
            return {
                "search_replace_blocks": state.get("search_replace_blocks", []),
                "retry_count": state["retry_count"] + 1,
            }

        tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []

        if not tool_calls:
            break

        messages.append(response)
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args", {})
            tool_id = tc.get("id", "")

            logger.info("Fixer 调用工具: %s(%s)", tool_name, json.dumps(tool_args, ensure_ascii=False)[:200])

            if tool_name in TOOL_MAP:
                try:
                    result = await TOOL_MAP[tool_name].ainvoke(tool_args)
                except Exception as e:
                    result = f"工具执行失败: {e}"
            else:
                result = f"未知工具: {tool_name}"

            messages.append(ToolMessage(content=str(result), tool_call_id=tool_id))

    # ─────────────────────────────────────────
    # 最终结构化输出
    # ─────────────────────────────────────────
    structured_llm = llm.with_structured_output(FixerOutput)

    final_messages = [
        system_msg,
        human_msg,
        HumanMessage(
            content=(
                "基于上述分析和工具调用结果，请输出最终的 Search/Replace Block。"
                "确保 search 字符串与源文件中的内容精确匹配。"
            )
        ),
    ]

    try:
        output: FixerOutput = await structured_llm.ainvoke(final_messages)
    except Exception as e:
        logger.error("Fixer 结构化输出失败: %s", e)
        return {
            "search_replace_blocks": state.get("search_replace_blocks", []),
            "retry_count": state["retry_count"] + 1,
        }

    logger.info(
        "Fixer 完成: %d 个 Search/Replace Block, explanation=%s",
        len(output.blocks),
        output.explanation[:100] if output.explanation else "",
    )

    blocks: List[Dict[str, Any]] = [
        {
            "file_path": block.file_path,
            "search_block": block.search_block,
            "replace_block": block.replace_block,
            "context_before": block.context_before,
            "context_after": block.context_after,
        }
        for block in output.blocks
    ]

    return {
        "search_replace_blocks": blocks,
        "retry_count": state["retry_count"] + 1,
    }


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
