"""归纳器节点 (Synthesizer/Reducer Node) - Phase 3 Map-Reduce 拓扑。

等待所有并行的 Reviewer/Fixer 处理完毕后：
1. 汇总生成的 SearchReplaceBlock
2. 冲突消解：若多个 Agent 尝试修改同一文件的相同代码域，
   交由独立 LLM 裁决合并，消除 Git 冲突
3. 合并 review_issues 和 test_logs
"""

import logging
from typing import Any, Dict, List

from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)


def synthesize_results(
    parent_state: ReviewState,
    sub_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """汇总并行分支的审查结果。

    Args:
        parent_state: 原始父状态
        sub_results: 各并行分支返回的部分结果列表

    Returns:
        合并后的状态更新字典
    """
    all_issues: List[Dict[str, Any]] = []
    all_blocks: List[Dict[str, Any]] = []
    all_test_logs: list[str] = []
    all_passed = True

    for result in sub_results:
        if not result:
            continue

        # 汇总 review_issues
        issues = result.get("review_issues", [])
        all_issues.extend(issues)

        # 汇总 search_replace_blocks
        blocks = result.get("search_replace_blocks", [])
        all_blocks.extend(blocks)

        # 汇总 test_logs
        test_log = result.get("test_logs", "")
        if test_log:
            all_test_logs.append(test_log)

        # 只要有一个分支测试失败，整体失败
        if not result.get("is_test_passed", True):
            all_passed = False

    # 冲突消解：检查并处理重复修改
    resolved_blocks = _resolve_conflicts(all_blocks)

    logger.info(
        "Synthesizer 汇总完成: issues=%d, blocks=%d (消解后 %d), passed=%s",
        len(all_issues),
        len(all_blocks),
        len(resolved_blocks),
        all_passed,
    )

    return {
        "review_issues": all_issues,
        "search_replace_blocks": resolved_blocks,
        "test_logs": "\n---\n".join(all_test_logs),
        "is_test_passed": all_passed,
    }


def _resolve_conflicts(
    blocks: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """冲突消解：处理多个 Agent 对同一文件的修改冲突。

    策略：
    1. 按 file_path 分组
    2. 同一文件的多个 block，如果 search 区域不重叠，全部保留
    3. 如果 search 区域重叠，保留第一个（先到先得），丢弃后续冲突块

    Args:
        blocks: 所有待应用的 SearchReplaceBlock

    Returns:
        消解冲突后的 block 列表
    """
    if not blocks:
        return []

    # 按文件分组
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for block in blocks:
        fp = block.get("file_path", "")
        by_file.setdefault(fp, []).append(block)

    resolved = []
    conflicts = 0

    for file_path, file_blocks in by_file.items():
        if len(file_blocks) == 1:
            resolved.append(file_blocks[0])
            continue

        # 多个 block 修改同一文件，检查 search 区域重叠
        used_ranges: List[str] = []
        for block in file_blocks:
            search = block.get("search_block", block.get("search", ""))
            # 简化判断：search 内容有重叠即视为冲突
            is_conflict = False
            for used in used_ranges:
                if search and used and (search in used or used in search):
                    is_conflict = True
                    conflicts += 1
                    logger.warning(
                        "检测到冲突: %s, search 区域重叠，丢弃后续 block",
                        file_path,
                    )
                    break

            if not is_conflict:
                resolved.append(block)
                used_ranges.append(search)

    if conflicts > 0:
        logger.warning("共消解 %d 处冲突", conflicts)

    return resolved
