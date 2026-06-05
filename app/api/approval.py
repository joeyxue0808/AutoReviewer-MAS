"""审批 API - Human-in-the-Loop (Implementation Guide Phase 3 Task 3.3)。

提供审批端点，Tech Lead 点击"允许提交"后调用，
唤醒挂起的 Graph 完成 GitLab/GitHub 提交。

核心机制：
- Graph 编译时配置 `interrupt_before=["submit_node"]`
- Graph 执行到 submit_node 前自动挂起，状态持久化到 Postgres
- Tech Lead 调用 `/api/v1/approve/{thread_id}` 后，
  执行 `graph.update_state(thread_id, {"approval": True})` 恢复图流转
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from app.agents.workflow import compile_graph
from app.infra.checkpointer import get_checkpointer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["approval"])

# 待审批 thread_id 存储（生产环境应持久化到 Redis/DB）
_pending_approvals: Dict[str, Dict[str, Any]] = {}


def register_pending_approval(
    thread_id: str,
    pr_id: str,
    vcs_provider: str,
    high_risk_files: list[str],
) -> None:
    """注册待审批任务（由 Worker 调用）。"""
    _pending_approvals[thread_id] = {
        "thread_id": thread_id,
        "pr_id": pr_id,
        "vcs_provider": vcs_provider,
        "high_risk_files": high_risk_files,
        "status": "pending",
    }
    logger.info("已注册待审批: thread=%s, pr=%s", thread_id, pr_id)


@router.post("/approve/{thread_id}")
async def approve(thread_id: str) -> Dict[str, str]:
    """批准提交，通过 update_state 唤醒挂起的 Graph。

    实现机制（Implementation Guide Phase 3 Task 3.3）：
    1. 获取 checkpointer 和编译后的 graph
    2. 调用 graph.update_state() 注入 {"approval": True}
    3. 调用 graph.ainvoke(None) 恢复执行
    """
    if thread_id not in _pending_approvals:
        raise HTTPException(status_code=404, detail=f"未找到待审批任务: {thread_id}")

    logger.info("收到批准: thread=%s", thread_id)

    checkpointer = get_checkpointer()
    if not checkpointer:
        raise HTTPException(status_code=500, detail="Checkpointer 未启用，无法恢复 Graph")

    graph = compile_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    try:
        # 注入审批标记并恢复图流转
        graph.update_state(config, {"approval": True})
        await graph.ainvoke(None, config=config)

        _pending_approvals[thread_id]["status"] = "approved"
        logger.info("Graph 已唤醒并完成提交: thread=%s", thread_id)

        return {"status": "approved", "thread_id": thread_id}

    except Exception as e:
        logger.error("唤醒 Graph 失败: thread=%s, error=%s", thread_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to resume graph: {e}")


@router.post("/reject/{thread_id}")
async def reject(thread_id: str) -> Dict[str, str]:
    """拒绝提交，终止 Graph 执行。"""
    if thread_id not in _pending_approvals:
        raise HTTPException(status_code=404, detail=f"未找到待审批任务: {thread_id}")

    logger.info("收到拒绝: thread=%s", thread_id)

    _pending_approvals[thread_id]["status"] = "rejected"

    info = _pending_approvals[thread_id]
    try:
        from app.vcs.gitlab_client import GitLabProvider
        from app.vcs.github_client import GitHubProvider
        from app.vcs.base import CommentPayload

        provider = GitLabProvider() if info["vcs_provider"] == "gitlab" else GitHubProvider()
        await provider.post_comment(
            repo_id="",
            pr_id=info["pr_id"],
            comments=[CommentPayload(body="❌ **AutoReviewer 审批被拒绝**\n\n高危操作未通过人工审批，提交已取消。")],
        )
        await provider.close()
    except Exception as e:
        logger.error("发送拒绝通知失败: %s", e)

    return {"status": "rejected", "thread_id": thread_id}


@router.get("/approval/pending")
async def list_pending() -> list[Dict[str, Any]]:
    """查询待审批列表。"""
    return [v for v in _pending_approvals.values() if v["status"] == "pending"]
