"""Reviewer Agent 节点 - Phase 2 Tool Calling + ReAct 循环。

V2 变更：
- 读取 diff_chunks / detected_languages / repo_context
- 返回 List[Dict]

Phase 2 变更：
- 绑定 MCP 工具 (read_file_context / ast_find_references / list_directory)
- ReAct 推理循环：LLM 可主动调用工具获取代码上下文
- 最终通过 structured output 输出 ReviewerOutput
"""

import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.prompts.reviewer import REVIEWER_HUMAN_PROMPT, REVIEWER_SYSTEM_PROMPT
from app.core.llm_factory import get_llm
from app.schemas.llm_out import ReviewerOutput
from app.schemas.state import ReviewState
from app.tools import read_file_context, ast_find_references, list_directory

logger = logging.getLogger(__name__)

# Phase 2: 工具注册表
TOOLS = [read_file_context, ast_find_references, list_directory]
TOOL_MAP = {t.name: t for t in TOOLS}

# ReAct 循环最大迭代次数（防止无限循环）
MAX_TOOL_ITERATIONS = 8


async def reviewer_node(state: ReviewState) -> Dict[str, Any]:
    """Reviewer Agent 节点函数。

    Phase 2 ReAct 循环：
    1. LLM 分析 Diff，可能主动调用工具获取更多上下文
    2. 工具执行结果反馈给 LLM
    3. 重复直到 LLM 不再调用工具，输出最终审查结果
    4. 通过 .with_structured_output 强制 JSON 格式
    """
    detected = state.get("detected_languages", [])
    logger.info(
        "Reviewer 节点开始执行: vcs=%s, pr=%s, languages=%s",
        state.get("vcs_provider", "?"),
        state.get("pr_id", "?"),
        detected,
    )

    # 获取 LLM 并绑定工具（trace_id 关联 Langfuse 监控）
    trace_id = f"{state.get('vcs_provider', 'cli')}-{state.get('pr_id', 'local')}"
    llm = get_llm("reviewer", trace_id=trace_id)
    llm_with_tools = llm.bind_tools(TOOLS)

    # 构建 diff_chunks 可读文本
    diff_chunks = state.get("diff_chunks", {})
    diff_text = ""
    for lang, chunk in diff_chunks.items():
        diff_text += f"\n### [{lang}]\n```diff\n{chunk}\n```\n"

    # 初始消息
    system_msg = SystemMessage(content=REVIEWER_SYSTEM_PROMPT)
    human_msg = HumanMessage(
        content=REVIEWER_HUMAN_PROMPT.format(
            languages=", ".join(detected) if detected else "unknown",
            repo_context=state.get("repo_context", "(无仓库上下文)"),
            diff_chunks=diff_text or "(无 diff 内容)",
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
            logger.error("Reviewer LLM 调用失败 (iteration %d): %s", iteration, e)
            return {"review_issues": []}

        # 检查是否有 tool_calls
        tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []

        if not tool_calls:
            # LLM 不再调用工具，ReAct 循环结束
            logger.info("ReAct 循环完成，共 %d 次迭代", iteration)
            break

        # 执行工具调用
        messages.append(response)  # 添加 AI 的 tool_calls 消息

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool_args = tc.get("args", {})
            tool_id = tc.get("id", "")

            logger.info("调用工具: %s(%s)", tool_name, json.dumps(tool_args, ensure_ascii=False)[:200])

            if tool_name in TOOL_MAP:
                try:
                    result = await TOOL_MAP[tool_name].ainvoke(tool_args)
                except Exception as e:
                    result = f"工具执行失败: {e}"
            else:
                result = f"未知工具: {tool_name}"

            messages.append(ToolMessage(content=str(result), tool_call_id=tool_id))
    else:
        logger.warning("ReAct 循环达到最大迭代次数 (%d)", MAX_TOOL_ITERATIONS)

    # ─────────────────────────────────────────
    # 最终结构化输出
    # ─────────────────────────────────────────

    # 使用 structured output 强制 ReviewerOutput 格式
    structured_llm = llm.with_structured_output(ReviewerOutput)

    # 将 ReAct 对话历史压缩为最终 prompt
    final_messages = [
        system_msg,
        human_msg,
        HumanMessage(
            content=(
                "基于上述分析和工具调用结果，请输出最终的审查结论。"
                "如果没有发现 critical 级别问题，设置 is_approved = True。"
            )
        ),
    ]

    try:
        output: ReviewerOutput = await structured_llm.ainvoke(final_messages)
    except Exception as e:
        logger.error("Reviewer 结构化输出失败: %s", e)
        return {"review_issues": []}

    logger.info(
        "Reviewer 完成: 发现 %d 个问题, is_approved=%s",
        len(output.issues),
        output.is_approved,
    )

    # 转换为 dict 列表
    review_issues: List[Dict[str, Any]] = [
        {
            "file_path": issue.file_path,
            "line_number": issue.line_number,
            "severity": issue.severity,
            "category": getattr(issue, "category", "general"),
            "description": issue.description,
            "suggestion": issue.suggestion,
        }
        for issue in output.issues
    ]

    return {"review_issues": review_issues}
