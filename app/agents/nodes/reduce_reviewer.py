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
    2. 去重（相同文件+行号+描述）：轮内 + 跨轮（对比 round_issues 历史）+ 跨会话（持久缓存）
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

    # 跨轮去重：收集历史 round_issues 的去重键
    historical_keys: set = set()
    for issue in state.get("round_issues", []):
        if isinstance(issue, dict):
            historical_keys.add((
                issue.get("file_path", ""),
                issue.get("line_number", 0),
                issue.get("description", ""),
            ))
        else:
            historical_keys.add((
                getattr(issue, "file_path", ""),
                getattr(issue, "line_number", 0),
                getattr(issue, "description", ""),
            ))

    # 跨会话去重：从持久缓存读取已知问题
    repo_id = state.get("repo_id", "")
    if repo_id:
        try:
            from app.core.persistent_cache import get_persistent_cache
            persistent_issues = get_persistent_cache(repo_id).get_known_issues()
            before = len(historical_keys)
            historical_keys.update(persistent_issues)
            added = len(historical_keys) - before
            if added:
                logger.info("跨会话去重: 加载 %d 个历史已知问题", added)
        except Exception:
            pass

    # 轮内去重 + 跨轮去重
    seen = set()
    unique_issues: List[Dict[str, Any]] = []
    skipped_historical = 0

    for issue in all_issues:
        if isinstance(issue, dict):
            fp = issue.get("file_path", "")
            ln = issue.get("line_number", 0)
            desc = issue.get("description", "")
        else:
            fp = getattr(issue, "file_path", "")
            ln = getattr(issue, "line_number", 0)
            desc = getattr(issue, "description", "")

        dedup_key = (fp, ln, desc)

        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        if dedup_key in historical_keys:
            skipped_historical += 1
            continue

        unique_issues.append(issue)

    if skipped_historical:
        logger.info("跨轮去重: 过滤 %d 个历史已报告问题", skipped_historical)

    def _get_severity(issue):
        if isinstance(issue, dict):
            return issue.get("severity", "info")
        return getattr(issue, "severity", "info")

    def sort_key(issue):
        return _SEVERITY_ORDER.get(_get_severity(issue), 99)

    sorted_issues = sorted(unique_issues, key=sort_key)

    def _normalize_severity(severity):
        severity = severity.lower() if isinstance(severity, str) else "info"
        return severity if severity in _SEVERITY_ORDER else "info"

    severity_counts = {"critical": 0, "warning": 0, "info": 0}
    for issue in sorted_issues:
        sev = _normalize_severity(_get_severity(issue))
        severity_counts[sev] += 1

    logger.info(
        "Reduce Reviewer: 去重后 %d 个问题 (critical=%d, warning=%d, info=%d)",
        len(sorted_issues),
        severity_counts["critical"],
        severity_counts["warning"],
        severity_counts["info"],
    )

    # 将本次新发现的问题写入持久缓存
    repo_id = state.get("repo_id", "")
    if repo_id:
        try:
            from app.core.persistent_cache import get_persistent_cache
            get_persistent_cache(repo_id).add_known_issues(sorted_issues)
        except Exception:
            pass

    return {"review_issues": sorted_issues, "deduplicated_issues": sorted_issues}


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
