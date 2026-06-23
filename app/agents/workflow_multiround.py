"""LangGraph 状态图编排 - 多轮审查工作流。

多轮审查流程：
    router_node → Send(reviewer_node) × N → reduce_reviewer
                       ↓
              user_checkpoint_node ← 用户交互
                       ↓
              fixer_node → critic_node
                       ↓
              decision_node → 继续下一轮 or submit_node

特性：
1. 用户可以在任意节点暂停并提供指令
2. 支持自动多轮循环直到问题解决或达到最大轮次
3. 智能收敛检测，避免无效循环
"""

import logging
from typing import Any, Dict, List, Literal

from langgraph.graph import END, StateGraph
from langgraph.types import Send

from app.agents.nodes.critic import critic_node
from app.agents.nodes.decision import (
    after_critic,
    after_reviewer_multiround,
    decision_node,
    make_decision,
)
from app.agents.nodes.error_recovery import error_recovery_node
from app.agents.nodes.fixer import fixer_node
from app.agents.nodes.reduce_reviewer import reduce_reviewer_node
from app.agents.nodes.reviewer import reviewer_node
from app.agents.nodes.tester import tester_node
from app.agents.nodes.user_checkpoint import (
    after_user_checkpoint,
    user_checkpoint_node,
)
from app.core.config import settings
from app.core.diff_analyzer import DiffAnalyzer, DiffChunk
from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)

# DiffAnalyzer 实例（用于 router 切片）
_analyzer = DiffAnalyzer()


# ─────────────────────────────────────────────
# Router 节点：动态切片 + Send API 分发
# ─────────────────────────────────────────────


def router_node(state: ReviewState) -> List[Dict[str, Any]]:
    """路由决策节点：使用 LangGraph Send API 实现动态 Map-Reduce。

    根据 diff_chunks 的大小决定：
    - 大 MR：切分为多个 DiffChunk，通过 Send 并发分发给多个 reviewer_node
    - 小 MR：直接发送给单个 reviewer_node
    """
    diff_chunks = state.get("diff_chunks", {})
    detected = state.get("detected_languages", [])
    
    if not diff_chunks:
        return [Send("reviewer_node", _make_sub_state(state, "__empty__", "", detected))]
    
    # 合并所有 diff 内容
    full_diff = "\n".join(diff_chunks.values())
    
    # 使用 DiffAnalyzer 按 token 上限切片
    chunks = _analyzer.chunk_diff(full_diff)
    
    if len(chunks) <= 1:
        # 小 MR，单 reviewer 处理
        chunk = chunks[0] if chunks else DiffChunk("__empty__", "", "", 0, 0)
        logger.info("Router: 小 MR，单 reviewer 处理 (%s)", chunk.chunk_id)
        return [Send("reviewer_node", _make_sub_state(state, chunk.chunk_id, chunk.content, [chunk.language]))]
    
    # 大 MR，按 Chunk 并发分发
    logger.info("Router: 大 MR，%d 个 Chunk 并发分发", len(chunks))
    sends = []
    for chunk in chunks:
        sends.append(
            Send("reviewer_node", _make_sub_state(state, chunk.chunk_id, chunk.content, [chunk.language]))
        )
    return sends


def _make_sub_state(
    parent: ReviewState,
    chunk_id: str,
    diff_content: str,
    languages: List[str],
) -> Dict[str, Any]:
    """构建发送给 reviewer_node 的子状态。"""
    return {
        "vcs_provider": parent.get("vcs_provider", ""),
        "pr_id": parent.get("pr_id", ""),
        "trigger_type": parent.get("trigger_type", "webhook_pr"),
        "repo_id": parent.get("repo_id", ""),
        "repo_context": parent.get("repo_context", ""),
        "diff_chunks": {chunk_id: diff_content} if diff_content else {},
        "detected_languages": languages,
        "review_issues": [],
        "search_replace_blocks": [],
        "test_logs": "",
        "is_test_passed": False,
        "retry_count": 0,
        "error_count": 0,
        "error_type": "",
        "last_node": "",
        # 多轮审查字段
        "current_round": parent.get("current_round", 0),
        "max_rounds": parent.get("max_rounds", settings.max_retry_count),
        "user_input_queue": parent.get("user_input_queue"),
        "user_instructions": parent.get("user_instructions", ""),
        "user_decisions": parent.get("user_decisions", {}),
        "pending_user_approval": False,
        "user_approval_result": None,
        "fixed_issues": parent.get("fixed_issues", []),
        "remaining_issues": parent.get("remaining_issues", []),
        "round_reports": parent.get("round_reports", []),
    }


def _after_tester_multiround(state: ReviewState) -> Literal["decision_node", "submit_node"]:
    """测试节点后的路由逻辑（多轮版本）。"""
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)
    max_retries = settings.max_retry_count
    
    # 测试通过，进入决策节点检查是否需要继续下一轮
    if is_passed:
        logger.info("测试通过，进入决策节点")
        return "decision_node"
    
    # 达到重试上限，强制提交
    if retry_count >= max_retries:
        logger.warning(
            "已达最大重试次数 (%d/%d)，强制提交",
            retry_count, max_retries,
        )
        return "submit_node"
    
    # 测试未通过，回到修复节点
    logger.info("测试未通过，回到修复节点 (%d/%d)", retry_count, max_retries)
    return "fixer_node"


def submit_node(state: ReviewState) -> Dict[str, Any]:
    """提交节点 - 生成最终报告并返回。"""
    from app.agents.workflow import _format_review_report
    
    logger.info("提交节点: 生成最终报告")
    
    # 生成报告
    report = _format_review_report(state)
    
    # 添加多轮审查统计
    current_round = state.get("current_round", 0)
    fixed_issues = state.get("fixed_issues", [])
    round_reports = state.get("round_reports", [])
    
    stats = {
        "total_rounds": current_round + 1,
        "fixed_issues_count": len(fixed_issues),
        "round_reports": round_reports,
    }
    
    logger.info(
        "多轮审查完成: %d 轮, 修复 %d 个问题",
        stats["total_rounds"],
        stats["fixed_issues_count"],
    )
    
    return {
        "review_report": report,
        "review_stats": stats,
    }


def build_multiround_graph() -> StateGraph:
    """构建支持多轮循环的工作流。"""
    graph = StateGraph(ReviewState)
    
    # 添加节点
    graph.add_node("reviewer_node", reviewer_node)
    graph.add_node("reduce_reviewer_node", reduce_reviewer_node)
    graph.add_node("fixer_node", fixer_node)
    graph.add_node("critic_node", critic_node)
    graph.add_node("tester_node", tester_node)
    graph.add_node("user_checkpoint_node", user_checkpoint_node)
    graph.add_node("decision_node", decision_node)
    graph.add_node("submit_node", submit_node)
    graph.add_node("error_recovery_node", error_recovery_node)
    
    # router 使用 Send API 动态分发
    graph.add_conditional_edges("__start__", router_node)
    
    # reviewer → reduce_reviewer（合并多个 reviewer 结果）
    graph.add_edge("reviewer_node", "reduce_reviewer_node")
    
    # reduce_reviewer → (fixer | user_checkpoint | error_recovery | END)
    graph.add_conditional_edges(
        "reduce_reviewer_node",
        after_reviewer_multiround,
        {
            "fixer_node": "fixer_node",
            "user_checkpoint_node": "user_checkpoint_node",
            "error_recovery_node": "error_recovery_node",
            "__end__": END,
        },
    )
    
    # user_checkpoint → (fixer | reviewer | submit | END)
    graph.add_conditional_edges(
        "user_checkpoint_node",
        after_user_checkpoint,
        {
            "fixer_node": "fixer_node",
            "reviewer_node": "reviewer_node",
            "submit_node": "submit_node",
            "__end__": END,
        },
    )
    
    # error_recovery → reviewer（恢复后重试）
    graph.add_edge("error_recovery_node", "reviewer_node")
    
    # fixer → critic
    graph.add_edge("fixer_node", "critic_node")
    
    # critic → (fixer | decision)
    graph.add_conditional_edges(
        "critic_node",
        after_critic,
        {
            "fixer_node": "fixer_node",
            "decision_node": "decision_node",
        },
    )
    
    # tester → (fixer | decision | submit) — 死循环硬阻断
    graph.add_conditional_edges(
        "tester_node",
        _after_tester_multiround,
        {
            "decision_node": "decision_node",
            "fixer_node": "fixer_node",
            "submit_node": "submit_node",
        },
    )
    
    # decision → (reviewer | submit | END)
    graph.add_conditional_edges(
        "decision_node",
        make_decision,
        {
            "reviewer_node": "reviewer_node",
            "submit_node": "submit_node",
            "__end__": END,
        },
    )
    
    # submit → END
    graph.add_edge("submit_node", END)
    
    return graph


def compile_multiround_graph(checkpointer=None, interrupt_before: list[str] | None = None):
    """编译多轮审查 Graph。"""
    graph_builder = build_multiround_graph()
    kwargs: dict = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer
    if interrupt_before is not None:
        kwargs["interrupt_before"] = interrupt_before
    else:
        kwargs["interrupt_before"] = ["user_checkpoint_node"]
    return graph_builder.compile(**kwargs)


# 默认多轮审查实例
multiround_graph = compile_multiround_graph()
