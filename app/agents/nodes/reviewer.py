"""Reviewer Agent 节点 - 极速模式。

核心优化：预加载 diff 涉及文件的上下文，一次性注入 prompt，
Reviewer 单次 LLM 调用完成审查，无需 ReAct 工具调用循环。

相比旧方案（20 轮 ReAct × 5s/轮 = 100s），
新方案仅需 1 次 LLM 调用（~10-15s）。
"""

import logging
import re
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.prompts.reviewer import REVIEWER_HUMAN_PROMPT, REVIEWER_SYSTEM_PROMPT
from app.core.llm_factory import get_llm
from app.schemas.llm_out import ReviewerOutput
from app.schemas.state import ReviewState
from app.tools import read_file_context

logger = logging.getLogger(__name__)

# 从 diff 中提取文件路径的正则
_DIFF_FILE_PATTERN = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

# 每个文件读取的最大行数（围绕变更区域）
_CONTEXT_LINES = 60


async def reviewer_node(state: ReviewState) -> Dict[str, Any]:
    """Reviewer Agent 节点函数 — 极速模式。

    1. 从 diff 中提取涉及的文件路径
    2. 并发读取所有文件的上下文（预加载）
    3. 一次性注入 prompt，单次 LLM 调用完成审查
    """
    detected = state.get("detected_languages", [])
    logger.info(
        "Reviewer 节点开始执行: vcs=%s, pr=%s, languages=%s",
        state.get("vcs_provider", "?"),
        state.get("pr_id", "?"),
        detected,
    )

    # ── Step 1: 从 diff 提取文件路径 + 变更行号 ──
    diff_chunks = state.get("diff_chunks", {})
    full_diff = "\n".join(diff_chunks.values())
    file_contexts = await _preload_file_contexts(full_diff)

    logger.info("预加载 %d 个文件上下文 (%d chars)", len(file_contexts), sum(len(v) for v in file_contexts.values()))

    # ── Step 2: 构建包含完整上下文的 prompt ──
    diff_text = ""
    for lang, chunk in diff_chunks.items():
        diff_text += f"\n### [{lang}]\n```diff\n{chunk}\n```\n"

    # 附加预加载的文件上下文
    context_text = ""
    for fp, content in file_contexts.items():
        context_text += f"\n### 文件: {fp}\n```python\n{content}\n```\n"

    repo_context = state.get("repo_context", "(无仓库上下文)")
    if len(repo_context) > 4000:
        repo_context = repo_context[:4000] + "\n... [截断]"

    system_msg = SystemMessage(content=REVIEWER_SYSTEM_PROMPT)
    human_msg = HumanMessage(
        content=REVIEWER_HUMAN_PROMPT.format(
            languages=", ".join(detected) if detected else "unknown",
            repo_context=repo_context,
            diff_chunks=diff_text or "(无 diff 内容)",
        )
        + "\n\n## 变更文件的完整上下文（已预加载，请直接分析，无需调用工具）\n"
        + context_text
    )

    # ── Step 3: 单次 LLM 调用，结构化输出 ──
    trace_id = f"{state.get('vcs_provider', 'cli')}-{state.get('pr_id', 'local')}"
    llm = get_llm("reviewer", trace_id=trace_id)
    structured_llm = llm.with_structured_output(ReviewerOutput, method="json_mode")

    try:
        output: ReviewerOutput = await structured_llm.ainvoke([system_msg, human_msg])
    except Exception as e:
        error_str = str(e)
        error_type = "unknown"
        if "429" in error_str:
            error_type = "429"
        elif "timeout" in error_str.lower():
            error_type = "timeout"
        elif "connection" in error_str.lower():
            error_type = "connection"
        logger.error("Reviewer LLM 调用失败: %s (type=%s)", e, error_type)
        # 不写入 error_type/last_node（并发 reviewer 会冲突）
        # review-only graph 中无 error_recovery 节点，错误仅记录日志
        return {
            "review_issues": [],
            "error_count": state.get("error_count", 0) + 1,
        }

    logger.info(
        "Reviewer 完成: 发现 %d 个问题, is_approved=%s",
        len(output.issues),
        output.is_approved,
    )

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


async def _preload_file_contexts(diff_text: str) -> Dict[str, str]:
    """从 diff 中提取文件路径，并发读取每个文件的变更区域上下文。

    返回 {file_path: file_content} 字典。
    """
    import asyncio

    files = _DIFF_FILE_PATTERN.findall(diff_text)
    if not files:
        return {}

    # 提取变更行号（@@ -a,b +c,d @@ 中的行号）
    changed_lines = _extract_changed_lines(diff_text)

    # 并发读取所有文件
    async def read_one(file_path: str) -> tuple[str, str]:
        lines = changed_lines.get(file_path, [])
        if lines:
            # 按连续行号分组为独立 hunk（间隔 > 20 行视为不同 hunk）
            hunks = _group_into_hunks(lines, gap=20)
            if len(hunks) == 1:
                # 单 hunk：围绕变更区域读取
                center = (hunks[0][0] + hunks[0][-1]) // 2
                start = max(1, center - _CONTEXT_LINES // 2)
                end = center + _CONTEXT_LINES // 2
                try:
                    content = await read_file_context.ainvoke({
                        "file_path": file_path,
                        "start_line": start,
                        "end_line": end,
                    })
                except Exception:
                    content = await read_file_context.ainvoke({"file_path": file_path})
            else:
                # 多 hunk：分别读取每个 hunk 的上下文
                parts = []
                for hunk in hunks:
                    center = (hunk[0] + hunk[-1]) // 2
                    start = max(1, center - _CONTEXT_LINES // 4)
                    end = center + _CONTEXT_LINES // 4
                    try:
                        part = await read_file_context.ainvoke({
                            "file_path": file_path,
                            "start_line": start,
                            "end_line": end,
                        })
                        parts.append(str(part))
                    except Exception:
                        pass
                content = "\n...\n".join(parts) if parts else f"(无法读取 {file_path})"
        else:
            try:
                content = await read_file_context.ainvoke({"file_path": file_path})
            except Exception:
                content = f"(无法读取 {file_path})"
        # 截断过长内容
        content = str(content)
        if len(content) > 4000:
            content = content[:4000] + "\n... [截断]"
        return file_path, content

    # 用 set 去重
    unique_files = list(set(fp for _, fp in files))
    tasks = [read_one(fp) for fp in unique_files]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    contexts = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        fp, content = r
        contexts[fp] = content

    return contexts


def _extract_changed_lines(diff_text: str) -> Dict[str, List[int]]:
    """从 diff 中提取每个文件的变更行号。"""
    result: Dict[str, List[int]] = {}
    current_file = None

    for line in diff_text.splitlines():
        # 匹配文件头
        m = re.match(r"^diff --git a/(.+?) b/(.+?)$", line)
        if m:
            current_file = m.group(2)
            result.setdefault(current_file, [])
            continue

        # 匹配 hunk 头
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m and current_file:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            result[current_file].extend(range(start, start + min(count, 30)))
            continue

    return result


def _group_into_hunks(lines: List[int], gap: int = 20) -> List[List[int]]:
    """将行号列表按连续性分组为独立 hunk。

    例如 [100, 101, 102, 5000, 5001] 在 gap=20 时分为两个 hunk。
    """
    if not lines:
        return []
    sorted_lines = sorted(set(lines))
    hunks: List[List[int]] = [[sorted_lines[0]]]
    for line in sorted_lines[1:]:
        if line - hunks[-1][-1] <= gap:
            hunks[-1].append(line)
        else:
            hunks.append([line])
    return hunks
