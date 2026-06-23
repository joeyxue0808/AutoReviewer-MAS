"""LangGraph 状态图编排 - Map-Reduce 并发拓扑。

流转逻辑：
    router_node → Send API 动态分发
        ├─ 大 MR → 并发多个 reviewer_node (每个处理一个 DiffChunk)
        └─ 小 MR → 单个 reviewer_node
                                                    ↓
                                              fixer_node → critic_node → tester_node
                                                ↑              ↓
                                                └── (reject) ──┘

死循环阻断：
    tester_node 之后，retry_count >= 3 时强制终止，
    降级到 submit_node 提交已有结果，从物理层面斩断无限重试环。
"""

import logging
import re
from typing import Any, Dict, List, Literal

from langgraph.graph import END, StateGraph

# 预切分 chunk_id 格式检测："{lang}_{idx}"，如 "python_0", "go_3"
_PRE_CHUNKED_RE = re.compile(r"^[a-z]+_\d+$")

from app.agents.nodes.critic import critic_node
from app.agents.nodes.error_recovery import error_recovery_node
from app.agents.nodes.fixer import fixer_node
from app.agents.nodes.reduce_reviewer import reduce_reviewer_node
from app.agents.nodes.reviewer import reviewer_node
from app.agents.nodes.tester import tester_node
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
    - 已预切分（CLI 传入的 chunk_id 格式如 "python_0"）：跳过重复切分

    Returns:
        Send 指令列表，每个元素是要发送给 reviewer_node 的状态片段
    """
    from langgraph.types import Send

    diff_chunks = state.get("diff_chunks", {})
    detected = state.get("detected_languages", [])

    if not diff_chunks:
        return [Send("reviewer_node", _make_sub_state(state, "__empty__", "", detected))]

    # 检查是否已预切分（chunk_id 格式为 "{lang}_{idx}"，如 "python_0"）
    if all(_PRE_CHUNKED_RE.match(k) for k in diff_chunks.keys()):
        # 已预切分，直接按 chunk 分发，不重复切分
        logger.info("Router: 检测到预切分 %d 个 Chunk，跳过重复切分", len(diff_chunks))
        sends = []
        for chunk_id, content in diff_chunks.items():
            lang = chunk_id.rsplit("_", 1)[0]
            sends.append(Send("reviewer_node", _make_sub_state(state, chunk_id, content, [lang])))
        return sends

    # 未预切分，合并后重新切分
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
    }


# ─────────────────────────────────────────────
# 条件路由函数
# ─────────────────────────────────────────────


def _after_reviewer(state: ReviewState) -> Literal["fixer_node", "error_recovery_node", "__end__"]:
    """Reviewer 之后：错误 → error_recovery，有 critical 问题 → fixer，否则 → END。"""
    # 错误恢复路由
    if state.get("error_type"):
        logger.info("Reviewer 出错 (%s)，进入 error_recovery", state["error_type"])
        return "error_recovery_node"

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
    """Critic 之后：通过 → tester，拒绝 → 回到 fixer（带重试上限）。

    注意：Fixer 的错误已在 fixer_node 内处理（设置 error_count），
    Critic 本身不会出错（纯规则检查），无需 error_recovery 路由。
    """
    blocks = state.get("search_replace_blocks", [])
    retry_count = state.get("retry_count", 0)
    error_count = state.get("error_count", 0)

    # 连续错误（429 等）达到上限，强制终止
    if error_count >= 3:
        logger.warning("连续错误达到上限 (%d)，强制降级提交", error_count)
        return "tester_node"

    if not blocks:
        if retry_count >= settings.max_retry_count:
            logger.warning("Critic: 无 blocks 且已达最大重试 (%d/%d)，降级提交", retry_count, settings.max_retry_count)
            return "tester_node"

        if state.get("test_logs", "").startswith("Critic 拒绝"):
            logger.info("Critic 拒绝，回到 fixer 重试 (%d/%d)", retry_count, settings.max_retry_count)
            return "fixer_node"

        logger.info("Critic: 无 blocks（Fixer 可能失败），进入 tester 降级")
        return "tester_node"

    logger.info("Critic 通过，进入 tester")
    return "tester_node"


def _after_tester(state: ReviewState) -> Literal["fixer_node", "submit_node"]:
    """Tester 之后的死循环硬阻断路由。

    规则（严格遵循 Implementation Guide Phase 1 Task 1.2）：
    - 测试通过 → submit_node
    - retry_count >= 3 → 强制终止，降级到 submit_node（提交已有结果）
    - retry_count < 3 且未通过 → 回到 fixer_node 重试
    """
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)
    max_retries = settings.max_retry_count

    if is_passed:
        logger.info("测试通过，进入 submit")
        return "submit_node"

    # 【硬阻断】物理层面斩断无限重试环
    if retry_count >= max_retries:
        logger.warning(
            "已达最大重试次数 (%d/%d)，强制终止，降级提交已有结果",
            retry_count, max_retries,
        )
        return "submit_node"

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
    retry_count = state.get("retry_count", 0)

    logger.info(
        "Submit 节点: vcs=%s, pr=%s, is_test_passed=%s, retry_count=%d",
        vcs_provider, pr_id,
        state.get("is_test_passed", False),
        retry_count,
    )

    # 如果是降级提交（达到重试上限），在报告中标注
    report = _format_review_report(state)

    # CLI 模式: 不需要提交到 VCS，直接返回报告
    if vcs_provider == "cli":
        logger.info("CLI 模式: 跳过 VCS 提交，返回本地审查报告")
        return {"review_report": report}

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

    # 降级提交标注
    if retry_count >= settings.max_retry_count and not is_passed:
        parts.append("> ⚠️ **降级提交**：已达最大重试次数，以下为未完全验证的审查结果")
        parts.append("")

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
# 构建并编译 StateGraph
# ─────────────────────────────────────────────


def build_graph() -> StateGraph:
    """构建 LangGraph StateGraph。

    拓扑：
        router → Send(reviewer) × N → reduce_reviewer → fixer → critic → tester → submit → END
                  ↑                                          ↑         ↓
                  └──────────────────────────────────────────┘         └── (retry < 3) ──┘
    """
    graph = StateGraph(ReviewState)

    # 添加节点
    graph.add_node("reviewer_node", reviewer_node)
    graph.add_node("reduce_reviewer_node", reduce_reviewer_node)
    graph.add_node("fixer_node", fixer_node)
    graph.add_node("critic_node", critic_node)
    graph.add_node("tester_node", tester_node)
    graph.add_node("submit_node", submit_node)
    graph.add_node("error_recovery_node", error_recovery_node)

    # router 使用 Send API 动态分发
    graph.add_conditional_edges("__start__", router_node)

    # reviewer → reduce_reviewer（合并多个 reviewer 结果）
    graph.add_edge("reviewer_node", "reduce_reviewer_node")

    # reduce_reviewer → (fixer | error_recovery | END)
    graph.add_conditional_edges(
        "reduce_reviewer_node",
        _after_reviewer,
        {"fixer_node": "fixer_node", "error_recovery_node": "error_recovery_node", "__end__": END},
    )

    # error_recovery → reviewer（恢复后重试）
    graph.add_edge("error_recovery_node", "reviewer_node")

    # fixer → critic
    graph.add_edge("fixer_node", "critic_node")

    # critic → (tester | fixer)
    graph.add_conditional_edges(
        "critic_node",
        _after_critic,
        {"tester_node": "tester_node", "fixer_node": "fixer_node"},
    )

    # tester → (fixer | submit) — 死循环硬阻断
    graph.add_conditional_edges(
        "tester_node",
        _after_tester,
        {"fixer_node": "fixer_node", "submit_node": "submit_node"},
    )

    # submit → END
    graph.add_edge("submit_node", END)

    return graph


def compile_graph(checkpointer=None, interrupt_before: list[str] | None = None):
    """编译 Graph，可选注入 Checkpointer 和 HITL 中断点。

    默认中断点：
    - fixer_node: 修复前需 Tech Lead 审批问题清单
    - submit_node: 提交前需审批高危文件

    CLI 模式传入 interrupt_before=[] 跳过所有审批。
    """
    graph_builder = build_graph()
    kwargs: dict = {}
    if checkpointer:
        kwargs["checkpointer"] = checkpointer
    if interrupt_before is not None:
        kwargs["interrupt_before"] = interrupt_before
    else:
        kwargs["interrupt_before"] = ["fixer_node", "submit_node"]
    return graph_builder.compile(**kwargs)


# 默认实例
app_graph = compile_graph()


# ─────────────────────────────────────────────
# 子图构建（用于 CLI 交互式审查）
# ─────────────────────────────────────────────


def build_review_only_graph() -> StateGraph:
    """构建仅包含审查阶段的子图。

    拓扑：router → Send(reviewer) → reduce_reviewer → END

    用于 CLI 交互模式：先审查拿到问题清单，
    用户选择后再执行修复阶段。
    """
    from app.agents.nodes.reduce_reviewer import reduce_reviewer_node

    graph = StateGraph(ReviewState)
    graph.add_node("reviewer_node", reviewer_node)
    graph.add_node("reduce_reviewer_node", reduce_reviewer_node)
    graph.add_conditional_edges("__start__", router_node)
    graph.add_edge("reviewer_node", "reduce_reviewer_node")
    graph.add_edge("reduce_reviewer_node", END)
    return graph


def build_fix_only_graph() -> StateGraph:
    """构建仅包含修复阶段的子图。

    拓扑：fixer → critic → tester → submit → END
          (critic/tester 可回退到 fixer，最多 3 轮)

    用于 CLI 交互模式：用户选择问题后执行修复。
    """
    graph = StateGraph(ReviewState)

    graph.add_node("fixer_node", fixer_node)
    graph.add_node("critic_node", critic_node)
    graph.add_node("tester_node", tester_node)
    graph.add_node("submit_node", submit_node)

    # __start__ → fixer
    graph.add_edge("__start__", "fixer_node")

    # fixer → critic
    graph.add_edge("fixer_node", "critic_node")

    # critic → (tester | fixer)
    graph.add_conditional_edges(
        "critic_node",
        _after_critic,
        {"tester_node": "tester_node", "fixer_node": "fixer_node"},
    )

    # tester → (fixer | submit)
    graph.add_conditional_edges(
        "tester_node",
        _after_tester,
        {"fixer_node": "fixer_node", "submit_node": "submit_node"},
    )

    # submit → END
    graph.add_edge("submit_node", END)

    return graph
