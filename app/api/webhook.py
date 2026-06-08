"""FastAPI Webhook 接口 - 消息队列削峰重构。

支持的事件：
- GitLab: merge_request (open/update/reopen), note (MR 评论)
- GitHub: pull_request (opened/synchronize/reopened), issue_comment (PR 评论)

评论触发命令：
- @autoreviewer review — 重新审查整个 PR
- @autoreviewer fix — 对审查问题执行自动修复
"""

import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from app.schemas.github import GitHubWebhookPayload
from app.schemas.gitlab import GitLabWebhookPayload
from app.vcs.base import DiffResult
from app.vcs.gitlab_client import GitLabProvider
from app.vcs.github_client import GitHubProvider
from app.infra.queue import review_queue

# Bot 命令前缀（PR 评论中 @bot 触发）
_BOT_MENTION = "autoreviewer"
_CMD_PATTERN = re.compile(
    rf"@{_BOT_MENTION}\s+(review|fix)(?:\s|$)",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhook", tags=["webhook"])


# ─────────────────────────────────────────────
# Webhook 签名验证
# ─────────────────────────────────────────────


def _verify_github_signature(payload_body: bytes, signature_header: str) -> bool:
    """验证 GitHub Webhook HMAC-SHA256 签名。"""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("GITHUB_WEBHOOK_SECRET 未设置，跳过签名验证")
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_gitlab_token(token_header: str) -> bool:
    """验证 GitLab Webhook Secret Token。"""
    secret = os.getenv("GITLAB_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("GITLAB_WEBHOOK_SECRET 未设置，跳过签名验证")
        return True
    if not token_header:
        return False
    return hmac.compare_digest(token_header, secret)


# ─────────────────────────────────────────────
# 核心：组装任务并推入队列
# ─────────────────────────────────────────────


def _parse_comment_command(comment_body: str) -> Optional[str]:
    """从评论内容中解析 bot 命令。

    Returns:
        命令字符串 ("review" / "fix") 或 None（非 bot 命令）
    """
    match = _CMD_PATTERN.search(comment_body)
    if match:
        return match.group(1).lower()
    return None


async def _enqueue_review_task(
    vcs_provider: str,
    repo_id: str,
    pr_id: str,
    trigger_type: str = "webhook_pr",
) -> None:
    """拉取 Diff 并将审查任务推入队列。"""
    provider = None
    try:
        provider = _create_vcs_provider(vcs_provider)

        logger.info("正在获取 %s %s!%s 的 diff...", vcs_provider, repo_id, pr_id)
        diff_result: DiffResult = await provider.get_diff(repo_id, pr_id)

        try:
            repo_map = await provider.get_repo_map(repo_id)
        except Exception as e:
            logger.warning("VCS Repo-Map 获取失败，降级为本地目录树: %s", e)
            from app.core.repo_mapper import generate_repo_map
            repo_map = generate_repo_map(".")

        diff_chunks: Dict[str, str] = {}
        for file_info in diff_result.files:
            lang = file_info.get("language")
            if lang:
                diff_chunks.setdefault(lang, "")
                diff_chunks[lang] += f"--- {file_info['file_path']}\n{file_info['diff']}\n"

        task = {
            "vcs_provider": vcs_provider,
            "pr_id": pr_id,
            "repo_id": repo_id,
            "trigger_type": trigger_type,
            "repo_context": repo_map,
            "diff_chunks": diff_chunks,
            "detected_languages": diff_result.languages_detected,
        }

        message_id = await review_queue.publish(task)
        logger.info("任务已入队: %s!%s, trigger=%s, msg_id=%s", vcs_provider, repo_id, trigger_type, message_id)

    except Exception as e:
        logger.error("任务入队失败: %s!%s, error=%s", vcs_provider, repo_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to enqueue task: {e}")
    finally:
        if provider:
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
    """接收 GitLab Webhook（MR 事件 + MR 评论事件）。"""
    token = request.headers.get("X-Gitlab-Token", "")
    if not _verify_gitlab_token(token):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    body = await _parse_body(request)
    object_kind = body.get("object_kind")

    # ── MR 事件 ──
    if object_kind == "merge_request":
        try:
            payload = GitLabWebhookPayload(**body)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Payload validation error: {e}")

        action = payload.object_attributes.action
        if action not in ("open", "update", "reopen"):
            return {"status": "ignored", "reason": f"action '{action}' not handled"}

        logger.info("收到 GitLab MR Webhook: project=%d, MR iid=%d, action=%s",
                     payload.project_id, payload.mr_iid, action)

        await _enqueue_review_task(
            vcs_provider="gitlab",
            repo_id=str(payload.project_id),
            pr_id=str(payload.mr_iid),
        )
        return {"status": "processing"}

    # ── MR 评论事件 ──
    if object_kind == "note":
        noteable_type = body.get("noteable_type", "")
        if noteable_type != "MergeRequest":
            return {"status": "ignored", "reason": f"note on {noteable_type}"}

        comment_body = body.get("object_attributes", {}).get("note", "")
        command = _parse_comment_command(comment_body)
        if not command:
            return {"status": "ignored", "reason": "not a bot command"}

        project_id = body.get("project", {}).get("id", "")
        mr_iid = body.get("merge_request", {}).get("iid", "")

        if not project_id or not mr_iid:
            return {"status": "ignored", "reason": "missing project/mr info"}

        trigger_type = f"webhook_comment:{command}"
        logger.info("收到 GitLab 评论命令: project=%s, MR=%s, command=%s", project_id, mr_iid, command)

        await _enqueue_review_task(
            vcs_provider="gitlab",
            repo_id=str(project_id),
            pr_id=str(mr_iid),
            trigger_type=trigger_type,
        )
        return {"status": "processing", "command": command}

    return {"status": "ignored", "reason": f"unhandled event: {object_kind}"}


# ─────────────────────────────────────────────
# GitHub Webhook
# ─────────────────────────────────────────────


@router.post("/github")
async def github_webhook(request: Request) -> Dict[str, str]:
    """接收 GitHub Webhook（PR 事件 + PR 评论事件）。"""
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    body = json.loads(raw_body)

    # 通过 GitHub Webhook-Event header 区分事件类型
    event_type = request.headers.get("X-GitHub-Event", "")

    # ── PR 事件 ──
    if event_type == "pull_request":
        action = body.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            return {"status": "ignored", "reason": f"action '{action}' not handled"}

        try:
            payload = GitHubWebhookPayload(**body)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Payload validation error: {e}")

        logger.info("收到 GitHub PR Webhook: repo=%s, PR #%d, action=%s",
                     payload.repo_full_name, payload.pr_number, action)

        await _enqueue_review_task(
            vcs_provider="github",
            repo_id=payload.repo_full_name,
            pr_id=str(payload.pr_number),
        )
        return {"status": "processing"}

    # ── PR 评论事件 ──
    if event_type == "issue_comment":
        action = body.get("action", "")
        if action != "created":
            return {"status": "ignored", "reason": f"comment action '{action}' not handled"}

        # 确认是对 PR 的评论（非普通 issue）
        issue = body.get("issue", {})
        if "pull_request" not in issue:
            return {"status": "ignored", "reason": "comment on issue, not PR"}

        comment_body = body.get("comment", {}).get("body", "")
        command = _parse_comment_command(comment_body)
        if not command:
            return {"status": "ignored", "reason": "not a bot command"}

        repo_full_name = body.get("repository", {}).get("full_name", "")
        pr_number = issue.get("number", "")

        if not repo_full_name or not pr_number:
            return {"status": "ignored", "reason": "missing repo/pr info"}

        trigger_type = f"webhook_comment:{command}"
        logger.info("收到 GitHub 评论命令: repo=%s, PR=#%s, command=%s",
                     repo_full_name, pr_number, command)

        await _enqueue_review_task(
            vcs_provider="github",
            repo_id=repo_full_name,
            pr_id=str(pr_number),
            trigger_type=trigger_type,
        )
        return {"status": "processing", "command": command}

    return {"status": "ignored", "reason": f"unhandled event: {event_type}"}


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────


async def _parse_body(request: Request) -> Dict[str, Any]:
    """解析请求 body 为 JSON。"""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
