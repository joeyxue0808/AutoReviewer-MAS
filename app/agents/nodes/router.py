"""动态路由节点 (Router Node) - Phase 3 Map-Reduce 并发拓扑。

当检测到巨型 MR（超过阈值）时，将 ReviewState 拆分为多个 SubState，
通过 LangGraph 的 Send API 并发广播给多个克隆的 reviewer_node。

触发条件（满足任一即触发并发路由）：
- 检测到 3 种以上编程语言
- Diff 包含超过 5 个文件
- 总 Diff 行数超过 1000 行

不满足阈值时，走传统的单 Agent 串行流程。
"""

import logging
from typing import Any, Dict, List

from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 路由阈值配置
# ─────────────────────────────────────────────
LANG_THRESHOLD = 3      # 语言数超过此值触发并发
FILE_THRESHOLD = 5      # 文件数超过此值触发并发
LINE_THRESHOLD = 1000   # Diff 总行数超过此值触发并发


def should_fan_out(state: ReviewState) -> bool:
    """判断是否需要触发 Map-Reduce 并发路由。

    Args:
        state: 当前 ReviewState

    Returns:
        True 表示需要并发拆分，False 表示走串行流程
    """
    detected = state.get("detected_languages", [])
    diff_chunks = state.get("diff_chunks", {})

    # 条件 1：语言数超过阈值
    if len(detected) >= LANG_THRESHOLD:
        logger.info(
            "触发并发路由: 语言数 %d >= %d",
            len(detected), LANG_THRESHOLD,
        )
        return True

    # 条件 2：文件数超过阈值
    total_files = sum(chunk.count("diff --git") for chunk in diff_chunks.values())
    if total_files >= FILE_THRESHOLD:
        logger.info(
            "触发并发路由: 文件数 %d >= %d",
            total_files, FILE_THRESHOLD,
        )
        return True

    # 条件 3：Diff 总行数超过阈值
    total_lines = sum(chunk.count("\n") for chunk in diff_chunks.values())
    if total_lines >= LINE_THRESHOLD:
        logger.info(
            "触发并发路由: Diff 行数 %d >= %d",
            total_lines, LINE_THRESHOLD,
        )
        return True

    return False


def build_sub_states(state: ReviewState) -> List[Dict[str, Any]]:
    """将大 ReviewState 拆分为多个 SubState。

    按语言拆分：每个 SubState 只包含一种语言的 diff_chunks，
    确保每个并发 Agent 的上下文纯净且可控。

    Args:
        state: 原始 ReviewState

    Returns:
        SubState 列表，每个元素是完整的 ReviewState 字典（只含单语言 diff）
    """
    detected = state.get("detected_languages", [])
    diff_chunks = state.get("diff_chunks", {})

    sub_states = []

    for lang in detected:
        if lang not in diff_chunks:
            continue

        sub_state: Dict[str, Any] = {
            "vcs_provider": state.get("vcs_provider", ""),
            "pr_id": state.get("pr_id", ""),
            "trigger_type": state.get("trigger_type", "webhook_pr"),
            "repo_context": state.get("repo_context", ""),
            "diff_chunks": {lang: diff_chunks[lang]},
            "detected_languages": [lang],
            "review_issues": [],
            "search_replace_blocks": [],
            "test_logs": "",
            "is_test_passed": False,
            "retry_count": 0,
        }
        sub_states.append(sub_state)

    logger.info(
        "已拆分为 %d 个 SubState: %s",
        len(sub_states),
        [s["detected_languages"][0] for s in sub_states],
    )

    return sub_states
