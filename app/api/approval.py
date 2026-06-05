"""审批 API - Phase 4 Human-in-the-Loop。

提供审批端点，Tech Lead 点击"允许提交"后调用，
唤醒挂起的 Graph 完成 GitLab/GitHub 提交。

端点：
- POST /api/v1/approve/{thread_id} — 批准提交
- POST /api/v1/reject/{thread_id} — 拒绝提交
- GET /api/v1/approval/pending — 查询待审批列表
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
    """批准提交，唤醒挂起的 Graph。

    Tech Lead 在飞书/钉钉卡片中点击"允许提交"后调用此端点。
    """
    if thread_id not in _pending_approvals:
        raise HTTPException(status_code=404, detail=f"未找到待审批任务: {thread_id}")

    logger.info("收到批准: thread=%s", thread_id)

    # 获取 checkpointer 并恢复 Graph
    checkpointer = get_checkpointer()
    if not checkpointer:
        raise HTTPException(status_code=500, detail="Checkpointer 未启用，无法恢复 Graph")

    graph = compile_graph(checkpointer=checkpointer)

    try:
        # 唤醒 Graph：从 submit_node 继续执行
        config = {"configurable": {"thread_id": thread_id}}
        # graph.update_state 不传新数据，仅标记为继续
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

    # 向 VCS 发送拒绝评论
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
