"""GitLab VCS Provider - 实现 BaseVCSProvider 抽象接口。

基于 aiohttp 的异步 GitLab REST API 客户端。
"""

import logging
import re
from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config import settings
from app.vcs.base import BaseVCSProvider, CommentPayload, DiffResult

logger = logging.getLogger(__name__)

# 文件后缀 → 语言映射
_EXT_LANG_MAP: Dict[str, str] = {
    ".go": "go",
    ".py": "python",
    ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".java": "java",
    ".vue": "vue",
    ".js": "javascript", ".cjs": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".dart": "flutter",
    ".cs": "csharp",
}


def _detect_language(file_path: str) -> Optional[str]:
    """根据文件后缀检测语言。"""
    for ext, lang in _EXT_LANG_MAP.items():
        if file_path.endswith(ext):
            return lang
    return None


class GitLabProvider(BaseVCSProvider):
    """GitLab VCS Provider 实现。"""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self._base_url = (base_url or settings.gitlab.api_url).rstrip("/")
        self._token = token or settings.gitlab.token
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "PRIVATE-TOKEN": self._token,
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ─────────────────────────────────────────
    # get_diff
    # ─────────────────────────────────────────

    async def get_diff(self, repo_id: str, pr_id: str) -> DiffResult:
        """获取 GitLab MR 的文件变更。"""
        url = f"{self._base_url}/projects/{repo_id}/merge_requests/{pr_id}/changes"
        session = await self._get_session()

        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"获取 MR 变更失败: status={resp.status}, body={text}")
            data = await resp.json()

        changes = data.get("changes", [])
        files: List[Dict[str, Any]] = []
        languages: set[str] = set()
        raw_diffs: list[str] = []

        for change in changes:
            file_path = change.get("new_path", "unknown")
            diff = change.get("diff", "")
            lang = _detect_language(file_path)

            if lang:
                languages.add(lang)

            files.append({
                "file_path": file_path,
                "diff": diff,
                "language": lang,
                "additions": change.get("additions", 0),
                "deletions": change.get("deletions", 0),
            })
            raw_diffs.append(f"--- a/{file_path}\n+++ b/{file_path}\n{diff}")

        return DiffResult(
            files=files,
            raw_diff="\n".join(raw_diffs),
            languages_detected=sorted(languages),
        )

    # ─────────────────────────────────────────
    # post_comment
    # ─────────────────────────────────────────

    async def post_comment(
        self,
        repo_id: str,
        pr_id: str,
        comments: List[CommentPayload],
    ) -> None:
        """向 GitLab MR 发表评论。"""
        session = await self._get_session()

        for comment in comments:
            if comment.file_path and comment.line_number:
                # 行内评论 → 使用 Discussion API
                url = f"{self._base_url}/projects/{repo_id}/merge_requests/{pr_id}/discussions"
                payload = {
                    "body": comment.body,
                    "position": {
                        "base_sha": "HEAD~1",
                        "start_sha": "HEAD~1",
                        "head_sha": "HEAD",
                        "position_type": "text",
                        "new_path": comment.file_path,
                        "new_line": comment.line_number,
                    },
                }
            else:
                # 普通评论 → Notes API
                url = f"{self._base_url}/projects/{repo_id}/merge_requests/{pr_id}/notes"
                payload = {"body": comment.body}

            async with session.post(url, json=payload) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    logger.error("发表评论失败: status=%d, body=%s", resp.status, text)

        logger.info("已向 MR !%s (项目 %s) 发表 %d 条评论", pr_id, repo_id, len(comments))

    # ─────────────────────────────────────────
    # apply_commit
    # ─────────────────────────────────────────

    async def apply_commit(
        self,
        repo_id: str,
        pr_id: str,
        branch: str,
        message: str,
        changes: Dict[str, str],
    ) -> str:
        """通过 GitLab Commits API 提交代码修改。"""
        url = f"{self._base_url}/projects/{repo_id}/repository/commits"
        session = await self._get_session()

        # 构建 GitLab Commit API 的 actions 格式
        actions = []
        for file_path, content in changes.items():
            actions.append({
                "action": "update",
                "file_path": file_path,
                "content": content,
            })

        payload = {
            "branch": branch,
            "commit_message": message,
            "actions": actions,
        }

        async with session.post(url, json=payload) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(f"提交代码失败: status={resp.status}, body={text}")
            data = await resp.json()
            commit_id = data.get("id", "unknown")
            logger.info("已提交代码: commit=%s, branch=%s", commit_id, branch)
            return commit_id

    # ─────────────────────────────────────────
    # get_repo_map
    # ─────────────────────────────────────────

    async def get_repo_map(self, repo_id: str, ref: str = "main") -> str:
        """获取 GitLab 仓库目录树。"""
        url = f"{self._base_url}/projects/{repo_id}/repository/tree"
        session = await self._get_session()
        params = {"ref": ref, "recursive": "true", "per_page": 100}

        entries: list[dict] = []
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return "(无法获取仓库结构)"
            entries = await resp.json()

        # 构建简要目录树
        tree_lines: list[str] = []
        for entry in entries:
            path = entry.get("path", "")
            entry_type = entry.get("type", "")
            prefix = "📁" if entry_type == "tree" else "📄"
            tree_lines.append(f"{prefix} {path}")

        return "\n".join(tree_lines[:200])  # 限制条目防止上下文爆炸
