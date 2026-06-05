"""LangGraph 状态图编排 - Phase 3 Map-Reduce 并发拓扑。

V2 流转逻辑：
    reviewer_node → (critical?) → fixer_node → tester_node → (pass/retry?)
                                        ↑           ↓
                                        └───────────┘

Phase 3 增强：
    router_node → (需要并发?)
        ├─ Yes → fan_out_node (多 reviewer 并行) → synthesizer_node
        └─ No  → reviewer_node
                                                    ↓
                                              fixer_node → critic_node → tester_node
                                                ↑              ↓
                                                └── (reject) ──┘
"""

import logging
from typing import Any, Dict, List, Literal

from langgraph.graph import END, StateGraph

from app.agents.nodes.critic import critic_node
from app.agents.nodes.fixer import fixer_node
from app.agents.nodes.reviewer import reviewer_node
from app.agents.nodes.router import build_sub_states, should_fan_out
from app.agents.nodes.synthesizer import synthesize_results
from app.agents.nodes.tester import tester_node
from app.core.config import settings
from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Router 节点
# ─────────────────────────────────────────────


async def router_node(state: ReviewState) -> Dict[str, Any]:
    """路由决策节点。

    判断是否需要触发 Map-Reduce 并发路由。
    结果存储在 state["_routing_mode"] 中供条件边使用。
    """
    if should_fan_out(state):
        logger.info("Router: 触发并发路由")
        return {"_routing_mode": "fan_out"}
    else:
        logger.info("Router: 走串行流程")
        return {"_routing_mode": "serial"}


# ─────────────────────────────────────────────
# Fan-Out 节点：并发执行多个 reviewer_node
# ─────────────────────────────────────────────


async def fan_out_node(state: ReviewState) -> Dict[str, Any]:
    """并发执行节点：拆分 SubState 并并发调用 reviewer_node + fixer_node。

    内部实现并发，不依赖 LangGraph Send API，
    使用 asyncio.gather 并发执行所有子任务。
    """
    import asyncio

    sub_states = build_sub_states(state)

    logger.info("Fan-Out: 并发处理 %d 个子任务", len(sub_states))

    async def _process_sub(sub_state: Dict[str, Any]) -> Dict[str, Any]:
        """处理单个子任务：reviewer → fixer。"""
        lang = sub_state.get("detected_languages", ["?"])[0]

        # Reviewer
        review_result = await reviewer_node(sub_state)
        sub_state.update(review_result)

        # 如果有 critical 问题，执行 fixer
        critical = [
            i for i in sub_state.get("review_issues", [])
            if (i.get("severity") if isinstance(i, dict) else getattr(i, "severity", "")) == "critical"
        ]
        if critical:
            fix_result = await fixer_node(sub_state)
            sub_state.update(fix_result)

        return sub_state

    # 并发执行所有子任务
    results = await asyncio.gather(
        *[_process_sub(sub) for sub in sub_states],
        return_exceptions=True,
    )

    # 过滤异常结果
    valid_results = [r for r in results if isinstance(r, dict)]

    # Synthesizer 汇总
    merged = synthesize_results(state, valid_results)
    return merged


# ─────────────────────────────────────────────
# 条件路由函数
# ─────────────────────────────────────────────


def _after_router(state: ReviewState) -> Literal["fan_out_node", "reviewer_node"]:
    """Router 之后的条件路由。"""
    mode = state.get("_routing_mode", "serial")
    if mode == "fan_out":
        return "fan_out_node"
    return "reviewer_node"


def _after_reviewer(state: ReviewState) -> Literal["fixer_node", "__end__"]:
    """Reviewer 之后：有 critical 问题 → fixer，否则 → END。"""
    critical_issues = [
        issue for issue in state.get("review_issues", [])
        if (issue.get("severity") if isinstance(issue, dict) else getattr(issue, "severity", "")) == "critical"
    ]

    if critical_issues:
        logger.info("发现 %d 个 critical 问题，进入 fixer 节点", len(critical_issues))
        return "fixer_node"

    logger.info("没有 critical 问题，审查通过，流程结束")
    return "__end__"


def _after_critic(state: ReviewState) -> Literal["tester_node", "fixer_node"]:
    """Critic 之后：通过 → tester，拒绝 → 回到 fixer。"""
    # 如果 blocks 被清空（Critic 拒绝），回到 fixer
    blocks = state.get("search_replace_blocks", [])
    if not blocks and state.get("test_logs", "").startswith("Critic 拒绝"):
        logger.info("Critic 拒绝，回到 fixer 重试")
        return "fixer_node"

    logger.info("Critic 通过，进入 tester")
    return "tester_node"


def _after_tester(state: ReviewState) -> Literal["fixer_node", "__end__"]:
    """Tester 之后：通过或达到重试上限 → END，否则 → fixer。"""
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)
    max_retries = settings.max_retry_count

    if is_passed:
        logger.info("测试通过，流程结束")
        return "__end__"

    if retry_count >= max_retries:
        logger.warning("已达最大重试次数 (%d/%d)，流程结束（降级）", retry_count, max_retries)
        return "__end__"

    logger.info("测试未通过，重试 (%d/%d): 回到 fixer 节点", retry_count, max_retries)
    return "fixer_node"


# ─────────────────────────────────────────────
# 提交节点
# ─────────────────────────────────────────────


async def submit_node(state: ReviewState) -> Dict[str, Any]:
    """提交节点 - 将审查结果写回 VCS 平台。"""
    from app.vcs.base import CommentPayload
    from app.vcs.gitlab_client import GitLabProvider
    from app.vcs.github_client import GitHubProvider

    vcs_provider = state.get("vcs_provider", "gitlab")
    pr_id = state.get("pr_id", "")

    logger.info(
        "Submit 节点: vcs=%s, pr=%s, is_test_passed=%s, retry_count=%d",
        vcs_provider, pr_id,
        state.get("is_test_passed", False),
        state.get("retry_count", 0),
    )

    report = _format_review_report(state)
    provider = _create_vcs_provider(vcs_provider)

    try:
        comments = [CommentPayload(body=report)]
        for issue in state.get("review_issues", []):
            fp = issue.get("file_path", "") if isinstance(issue, dict) else getattr(issue, "file_path", "")
            ln = issue.get("line_number", 0) if isinstance(issue, dict) else getattr(issue, "line_number", 0)
            desc = issue.get("description", "") if isinstance(issue, dict) else getattr(issue, "description", "")
            sug = issue.get("suggestion", "") if isinstance(issue, dict) else getattr(issue, "suggestion", "")
            sev = issue.get("severity", "info") if isinstance(issue, dict) else getattr(issue, "severity", "info")
            if fp and ln:
                comments.append(CommentPayload(
                    body=f"**{sev}**: {desc}\n\n建议: {sug}",
                    file_path=fp, line_number=ln,
                ))

        await provider.post_comment(repo_id=state.get("repo_id", ""), pr_id=pr_id, comments=comments)
        logger.info("已向 %s!%s 发表审查评论", vcs_provider, pr_id)
    except Exception as e:
        logger.error("发表评论失败: %s", e, exc_info=True)
    finally:
        await provider.close()

    return {}


def _create_vcs_provider(provider_name: str):
    import os
    if provider_name == "gitlab":
        return GitLabProvider()
    elif provider_name == "github":
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            raise ValueError("环境变量 GITHUB_TOKEN 未设置")
        return GitHubProvider(token=token)
    else:
        raise ValueError(f"不支持的 VCS 平台: {provider_name}")


def _format_review_report(state: ReviewState) -> str:
    issues = state.get("review_issues", [])
    test_logs = state.get("test_logs", "")
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)

    status_icon = "✅" if is_passed else "❌"
    parts = [f"## 🤖 AutoReviewer-MAS 审查报告 {status_icon}", ""]

    if issues:
        parts.append(f"### 发现 {len(issues)} 个问题 (重试 {retry_count} 次)")
        parts.append("")
        parts.append("| # | 级别 | 文件 | 行号 | 描述 | 建议 |")
        parts.append("|---|------|------|------|------|------|")
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "info") if isinstance(issue, dict) else getattr(issue, "severity", "info")
            fp = issue.get("file_path", "") if isinstance(issue, dict) else getattr(issue, "file_path", "")
            ln = issue.get("line_number", "") if isinstance(issue, dict) else getattr(issue, "line_number", "")
            desc = issue.get("description", "") if isinstance(issue, dict) else getattr(issue, "description", "")
            sug = issue.get("suggestion", "") if isinstance(issue, dict) else getattr(issue, "suggestion", "")
            badge = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(sev, sev)
            parts.append(f"| {i} | {badge} | `{fp}` | {ln} | {desc} | {sug} |")
        parts.append("")
    else:
        parts.append("### ✅ 未发现问题")
        parts.append("")

    if test_logs:
        parts.append("### 🧪 测试结果")
        parts.append("```")
        parts.append(test_logs[:2000])
        parts.append("```")
        parts.append("")

    parts.append("---")
    parts.append("*由 AutoReviewer-MAS 自动生成*")
    return "\n".join(parts)


# ─────────────────────────────────────────────
# 构建并编译 StateGraph (Phase 3 拓扑)
# ─────────────────────────────────────────────


def build_graph() -> StateGraph:
    """构建 Phase 3 LangGraph StateGraph。

    拓扑：
        router → (fan_out | serial reviewer) → fixer → critic → tester → submit
                  ↑                                     ↓
                  └──── (critic reject / test fail) ─────┘
    """
    graph = StateGraph(ReviewState)

    # 添加所有节点
    graph.add_node("router_node", router_node)
    graph.add_node("fan_out_node", fan_out_node)
    graph.add_node("reviewer_node", reviewer_node)
    graph.add_node("fixer_node", fixer_node)
    graph.add_node("critic_node", critic_node)
    graph.add_node("tester_node", tester_node)
    graph.add_node("submit_node", submit_node)

    # 入口
    graph.set_entry_point("router_node")

    # router → (fan_out | reviewer)
    graph.add_conditional_edges(
        "router_node",
        _after_router,
        {
            "fan_out_node": "fan_out_node",
            "reviewer_node": "reviewer_node",
        },
    )

    # fan_out → fixer（fan_out 内部已完成 reviewer+fixer，直接到 fixer 汇总）
    graph.add_edge("fan_out_node", "fixer_node")

    # reviewer → (fixer | END)
    graph.add_conditional_edges(
        "reviewer_node",
        _after_reviewer,
        {"fixer_node": "fixer_node", "__end__": END},
    )

    # fixer → critic
    graph.add_edge("fixer_node", "critic_node")

    # critic → (tester | fixer)
    graph.add_conditional_edges(
        "critic_node",
        _after_critic,
        {"tester_node": "tester_node", "fixer_node": "fixer_node"},
    )

    # tester → (fixer | submit → END)
    graph.add_conditional_edges(
        "tester_node",
        _after_tester,
        {"fixer_node": "fixer_node", "__end__": "submit_node"},
    )

    # submit → END
    graph.add_edge("submit_node", END)

    return graph


def compile_graph(checkpointer=None, interrupt_before: list[str] | None = None):
    """编译 Graph，可选注入 Checkpointer 和 HITL 中断点。

    Args:
        checkpointer: LangGraph Checkpointer 实例（Phase 1 持久化）
        interrupt_before: 在指定节点前挂起 Graph（Phase 4 HITL）
                          默认 ["submit_node"]，即提交前需要人工确认
    """
    graph_builder = build_graph()
    kwargs: dict = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer
    if interrupt_before is not None:
        kwargs["interrupt_before"] = interrupt_before
    else:
        # 默认在 submit_node 前挂起（HITL）
        kwargs["interrupt_before"] = ["submit_node"]
    return graph_builder.compile(**kwargs)


# 默认实例
app_graph = compile_graph()
