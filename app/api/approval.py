"""审批 API - Human-in-the-Loop (Implementation Guide Phase 3 Task 3.3)。

提供审批端点，Tech Lead 点击"允许提交"后调用，
唤醒挂起的 Graph 完成 GitLab/GitHub 提交。

核心机制：
- Graph 编译时配置 `interrupt_before=["submit_node"]`
- Graph 执行到 submit_node 前自动挂起，状态持久化到 Postgres
- Tech Lead 调用 `/api/v1/approve/{thread_id}` 后，
  执行 `graph.update_state(thread_id, {"approval": True})` 恢复图流转
- 审批状态持久化到 Redis Hash，支持多 Worker 和重启恢复
"""

import json
import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.infra.queue import review_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["approval"])

# Redis Hash key for pending approvals
_APPROVAL_KEY = "hitl:pending"


async def _get_redis():
    """获取 Redis 连接。"""
    return review_queue._redis


async def _store_approval(thread_id: str, data: dict) -> None:
    """持久化待审批到 Redis Hash。"""
    redis = await _get_redis()
    if redis:
        await redis.hset(_APPROVAL_KEY, thread_id, json.dumps(data, ensure_ascii=False))


async def _get_approval(thread_id: str) -> dict | None:
    """从 Redis 获取待审批。"""
    redis = await _get_redis()
    if redis:
        raw = await redis.hget(_APPROVAL_KEY, thread_id)
        if raw:
            return json.loads(raw)
    return None


async def _remove_approval(thread_id: str) -> None:
    """从 Redis 删除已处理的审批。"""
    redis = await _get_redis()
    if redis:
        await redis.hdel(_APPROVAL_KEY, thread_id)


async def _list_all_approvals() -> List[Dict[str, Any]]:
    """列出所有待审批。"""
    redis = await _get_redis()
    if redis:
        all_data = await redis.hgetall(_APPROVAL_KEY)
        return [json.loads(v) for v in all_data.values()]
    return []


def register_pending_approval(
    thread_id: str,
    pr_id: str,
    vcs_provider: str,
    high_risk_files: list[str],
) -> None:
    """注册待审批任务（由 Worker 调用，同步接口）。"""
    import asyncio
    data = {
        "thread_id": thread_id,
        "pr_id": pr_id,
        "vcs_provider": vcs_provider,
        "high_risk_files": high_risk_files,
        "status": "pending",
    }
    # 如果在 async 上下文中，直接存储；否则跳过（CLI 模式）
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_store_approval(thread_id, data))
    except RuntimeError:
        # 没有运行中的事件循环（如 CLI 模式），跳过持久化
        pass
    logger.info("已注册待审批: thread=%s, pr=%s", thread_id, pr_id)


@router.post("/approve/{thread_id}")
async def approve(thread_id: str) -> Dict[str, str]:
    """批准提交，通过 update_state 唤醒挂起的 Graph。"""
    approval = await _get_approval(thread_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"未找到待审批任务: {thread_id}")

    logger.info("收到批准: thread=%s", thread_id)

    from app.agents.workflow import compile_graph
    from app.infra.checkpointer import get_checkpointer

    checkpointer = get_checkpointer()
    if not checkpointer:
        raise HTTPException(status_code=500, detail="Checkpointer 未启用，无法恢复 Graph")

    async with checkpointer as cp:
        graph = compile_graph(checkpointer=cp)
        config = {"configurable": {"thread_id": thread_id}}

        try:
            graph.update_state(config, {"approval": True})
            await graph.ainvoke(None, config=config)

            approval["status"] = "approved"
            await _store_approval(thread_id, approval)
            logger.info("Graph 已唤醒并完成提交: thread=%s", thread_id)

            return {"status": "approved", "thread_id": thread_id}

        except Exception as e:
            logger.error("唤醒 Graph 失败: thread=%s, error=%s", thread_id, e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to resume graph: {e}")


@router.post("/reject/{thread_id}")
async def reject(thread_id: str) -> Dict[str, str]:
    """拒绝提交，终止 Graph 执行。"""
    approval = await _get_approval(thread_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"未找到待审批任务: {thread_id}")

    logger.info("收到拒绝: thread=%s", thread_id)

    approval["status"] = "rejected"
    await _store_approval(thread_id, approval)

    try:
        from app.vcs.gitlab_client import GitLabProvider
        from app.vcs.github_client import GitHubProvider
        from app.vcs.base import CommentPayload

        if approval["vcs_provider"] == "gitlab":
            provider = GitLabProvider()
        else:
            token = os.getenv("GITHUB_TOKEN", "")
            provider = GitHubProvider(token=token)

        await provider.post_comment(
            repo_id="",
            pr_id=approval["pr_id"],
            comments=[CommentPayload(body="❌ **AutoReviewer 审批被拒绝**\n\n高危操作未通过人工审批，提交已取消。")],
        )
        await provider.close()
    except Exception as e:
        logger.error("发送拒绝通知失败: %s", e)

    return {"status": "rejected", "thread_id": thread_id}


@router.get("/approval/pending")
async def list_pending() -> list[Dict[str, Any]]:
    """查询待审批列表。"""
    all_approvals = await _list_all_approvals()
    return [v for v in all_approvals if v.get("status") == "pending"]
