"""Reviewer Reduce 节点 - Map-Reduce 合并阶段。

当大 MR 被 router_node 切分为多个 DiffChunk 并发给多个 reviewer_node 后，
此节点负责合并所有审查结果，统一输出给 fixer_node。

Map-Reduce 流程：
    router_node → Send(reviewer_node) × N (Map 阶段)
                       ↓
              reduce_reviewer_node (Reduce 阶段)
                       ↓
                  fixer_node
"""

import logging
from typing import Any, Dict, List

from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)

# 严重级别优先级（用于排序）
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def reduce_reviewer_node(state: ReviewState) -> Dict[str, Any]:
    """Reviewer Reduce 节点 - 合并多个 reviewer 的审查结果。

    1. 收集所有 review_issues
    2. 去重（相同文件+行号+描述）
    3. 按严重级别排序（critical > warning > info）
    4. 合并输出
    """
    all_issues = state.get("review_issues", [])
    
    logger.info(
        "Reduce Reviewer: 合并 %d 个审查问题",
        len(all_issues),
    )
    
    if not all_issues:
        logger.info("Reduce Reviewer: 无审查问题，审查通过")
        return {"review_issues": []}
    
    # 去重：基于 file_path + line_number + description
    seen = set()
    unique_issues: List[Dict[str, Any]] = []
    
    for issue in all_issues:
        # 兼容 dict 和对象格式
        if isinstance(issue, dict):
            fp = issue.get("file_path", "")
            ln = issue.get("line_number", 0)
            desc = issue.get("description", "")
        else:
            fp = getattr(issue, "file_path", "")
            ln = getattr(issue, "line_number", 0)
            desc = getattr(issue, "description", "")
        
        # 生成去重键
        dedup_key = (fp, ln, desc[:100])  # 描述截断防止过长
        
        if dedup_key not in seen:
            seen.add(dedup_key)
            unique_issues.append(issue)
    
    # 按严重级别排序
    def sort_key(issue):
        if isinstance(issue, dict):
            severity = issue.get("severity", "info")
        else:
            severity = getattr(issue, "severity", "info")
        return _SEVERITY_ORDER.get(severity, 99)
    
    sorted_issues = sorted(unique_issues, key=sort_key)
    
    # 统计各级别数量
    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for issue in sorted_issues:
        if isinstance(issue, dict):
            severity = issue.get("severity", "info")
        else:
            severity = getattr(issue, "severity", "info")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    
    logger.info(
        "Reduce Reviewer: 去重后 %d 个问题 (critical=%d, warning=%d, info=%d)",
        len(sorted_issues),
        severity_counts["critical"],
        severity_counts["warning"],
        severity_counts["info"],
    )
    
    return {"review_issues": sorted_issues}


def _normalize_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    """标准化审查问题格式。"""
    return {
        "file_path": issue.get("file_path", ""),
        "line_number": issue.get("line_number", 0),
        "severity": issue.get("severity", "info"),
        "category": issue.get("category", "general"),
        "description": issue.get("description", ""),
        "suggestion": issue.get("suggestion", ""),
    }
