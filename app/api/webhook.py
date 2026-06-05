"""FastAPI Webhook 接口 - Phase 1 消息队列削峰重构。

废弃 BackgroundTasks，改为：
1. 接收 Webhook → 解析 Payload → 组装 ReviewState 初始结构
2. 推入 Redis Stream 队列 (mr_review_queue)
3. 立即返回 {"status": "processing"}
4. 独立 Worker 进程消费队列并执行 Graph

优势：
- Webhook 接口与 Graph 执行完全解耦
- Worker 崩溃后消息不丢失（Redis Stream ACK 机制）
- 支持多 Worker 水平扩展
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from app.schemas.github import GitHubWebhookPayload
from app.schemas.gitlab import GitLabWebhookPayload
from app.vcs.base import DiffResult
from app.vcs.gitlab_client import GitLabProvider
from app.vcs.github_client import GitHubProvider
from app.infra.queue import review_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])


# ─────────────────────────────────────────────
# 核心：组装任务并推入队列
# ─────────────────────────────────────────────


async def _enqueue_review_task(
    vcs_provider: str,
    repo_id: str,
    pr_id: str,
) -> None:
    """拉取 Diff 并将审查任务推入队列。

    注意：此处仅做轻量级 Diff 拉取和语言检测，
    重逻辑（LLM 审查、沙盒测试）全部由 Worker 消费后执行。
    """
    provider = _create_vcs_provider(vcs_provider)

    try:
        # 拉取 Diff（轻量操作）
        logger.info("正在获取 %s %s!%s 的 diff...", vcs_provider, repo_id, pr_id)
        diff_result: DiffResult = await provider.get_diff(repo_id, pr_id)

        # 获取 Repo Map
        repo_map = await provider.get_repo_map(repo_id)

        # 按语言拆分 diff_chunks
        diff_chunks: Dict[str, str] = {}
        for file_info in diff_result.files:
            lang = file_info.get("language")
            if lang:
                diff_chunks.setdefault(lang, "")
                diff_chunks[lang] += f"--- {file_info['file_path']}\n{file_info['diff']}\n"

        # 组装任务 payload
        task = {
            "vcs_provider": vcs_provider,
            "pr_id": pr_id,
            "repo_id": repo_id,
            "trigger_type": "webhook_pr",
            "repo_context": repo_map,
            "diff_chunks": diff_chunks,
            "detected_languages": diff_result.languages_detected,
        }

        # 推入队列
        message_id = await review_queue.publish(task)
        logger.info("任务已入队: %s!%s, msg_id=%s", vcs_provider, repo_id, message_id)

    except Exception as e:
        logger.error("任务入队失败: %s!%s, error=%s", vcs_provider, repo_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to enqueue task: {e}")
    finally:
        await provider.close()


def _create_vcs_provider(provider_name: str):
    """根据名称创建 VCS Provider 实例。"""
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


# ─────────────────────────────────────────────
# GitLab Webhook
# ─────────────────────────────────────────────


@router.post("/gitlab")
async def gitlab_webhook(request: Request) -> Dict[str, str]:
    """接收 GitLab Merge Request Webhook。

    流程：解析 → 校验 → 拉 Diff → 推队列 → 立即返回 200
    """
    body = await _parse_body(request)

    object_kind = body.get("object_kind")
    if object_kind != "merge_request":
        return {"status": "ignored", "reason": "not a merge_request event"}

    try:
        payload = GitLabWebhookPayload(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Payload validation error: {e}")

    action = payload.object_attributes.action
    if action not in ("open", "update", "reopen"):
        return {"status": "ignored", "reason": f"action '{action}' not handled"}

    logger.info(
        "收到 GitLab Webhook: project=%d, MR iid=%d, action=%s",
        payload.project_id, payload.mr_iid, action,
    )

    await _enqueue_review_task(
        vcs_provider="gitlab",
        repo_id=str(payload.project_id),
        pr_id=str(payload.mr_iid),
    )

    return {"status": "processing"}


# ─────────────────────────────────────────────
# GitHub Webhook
# ─────────────────────────────────────────────


@router.post("/github")
async def github_webhook(request: Request) -> Dict[str, str]:
    """接收 GitHub Pull Request Webhook。

    流程：解析 → 校验 → 拉 Diff → 推队列 → 立即返回 200
    """
    body = await _parse_body(request)

    action = body.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "reason": f"action '{action}' not handled"}

    try:
        payload = GitHubWebhookPayload(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Payload validation error: {e}")

    logger.info(
        "收到 GitHub Webhook: repo=%s, PR #%d, action=%s",
        payload.repo_full_name, payload.pr_number, action,
    )

    await _enqueue_review_task(
        vcs_provider="github",
        repo_id=payload.repo_full_name,
        pr_id=str(payload.pr_number),
    )

    return {"status": "processing"}


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────


async def _parse_body(request: Request) -> Dict[str, Any]:
    """解析请求 body 为 JSON。"""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
