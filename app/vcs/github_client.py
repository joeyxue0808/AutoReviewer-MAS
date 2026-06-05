"""GitHub VCS Provider - 实现 BaseVCSProvider 抽象接口。

基于 aiohttp 的异步 GitHub REST API 客户端。
repo_id 格式: "owner/repo"
"""

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from app.vcs.base import BaseVCSProvider, CommentPayload, DiffResult

logger = logging.getLogger(__name__)

# 文件后缀 → 语言映射（与 GitLab 保持一致）
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


class GitHubProvider(BaseVCSProvider):
    """GitHub VCS Provider 实现。"""

    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
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
        """获取 GitHub PR 的文件变更。"""
        # 获取 PR 文件列表
        url = f"{self._base_url}/repos/{repo_id}/pulls/{pr_id}/files"
        diff_url = f"{self._base_url}/repos/{repo_id}/pulls/{pr_id}"
        session = await self._get_session()

        async def _fetch_files():
            async with session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"获取 PR 文件失败: status={resp.status}, body={text}")
                return await resp.json()

        async def _fetch_diff():
            async with session.get(diff_url, headers={**self._headers, "Accept": "application/vnd.github.v3.diff"}) as resp:
                return await resp.text() if resp.status == 200 else ""

        files_data: list[dict] = await self._with_breaker(_fetch_files())
        raw_diff = await self._with_breaker(_fetch_diff())

        files: List[Dict[str, Any]] = []
        languages: set[str] = set()

        for f in files_data:
            file_path = f.get("filename", "unknown")
            lang = _detect_language(file_path)
            if lang:
                languages.add(lang)

            files.append({
                "file_path": file_path,
                "diff": f.get("patch", ""),
                "language": lang,
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
            })

        return DiffResult(
            files=files,
            raw_diff=raw_diff,
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
        """向 GitHub PR 发表评论（熔断器保护）。"""
        session = await self._get_session()

        head_sha = await self._with_breaker(
            self._get_pr_head_sha(repo_id, pr_id, session)
        )

        for comment in comments:
            if comment.file_path and comment.line_number and head_sha:
                url = f"{self._base_url}/repos/{repo_id}/pulls/{pr_id}/comments"
                payload = {
                    "body": comment.body,
                    "commit_id": head_sha,
                    "path": comment.file_path,
                    "line": comment.line_number,
                }
            else:
                url = f"{self._base_url}/repos/{repo_id}/issues/{pr_id}/comments"
                payload = {"body": comment.body}

            async def _post(u=url, p=payload):
                async with session.post(u, json=p) as resp:
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        raise RuntimeError(f"发表评论失败: status={resp.status}, body={text}")

            await self._with_breaker(_post())
                    logger.error("发表评论失败: status=%d, body=%s", resp.status, text)

        logger.info("已向 PR #%s (repo %s) 发表 %d 条评论", pr_id, repo_id, len(comments))

    async def _get_pr_head_sha(
        self, repo_id: str, pr_id: str, session: aiohttp.ClientSession
    ) -> Optional[str]:
        """获取 PR 的 head commit SHA。"""
        url = f"{self._base_url}/repos/{repo_id}/pulls/{pr_id}"
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("head", {}).get("sha")
        return None

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
        """通过 GitHub Contents API 逐文件提交。

        注意：GitHub REST API 没有批量 commit 接口，
        需要逐文件创建/更新 blob，然后创建 tree 和 commit。
        简化实现：逐文件调用 Contents API。
        """
        session = await self._get_session()
        last_sha = ""

        for file_path, content in changes.items():
            # 先获取文件的当前 SHA（更新需要）
            current_sha = await self._get_file_sha(repo_id, file_path, branch, session)

            url = f"{self._base_url}/repos/{repo_id}/contents/{file_path}"
            import base64
            payload: Dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if current_sha:
                payload["sha"] = current_sha

            async with session.put(url, json=payload) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise RuntimeError(f"提交文件 {file_path} 失败: status={resp.status}, body={text}")
                data = await resp.json()
                last_sha = data.get("commit", {}).get("sha", "unknown")

        logger.info("已提交 %d 个文件: branch=%s", len(changes), branch)
        return last_sha

    async def _get_file_sha(
        self, repo_id: str, file_path: str, ref: str, session: aiohttp.ClientSession
    ) -> Optional[str]:
        """获取文件的当前 SHA（用于更新操作）。"""
        url = f"{self._base_url}/repos/{repo_id}/contents/{file_path}"
        params = {"ref": ref}
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("sha")
        return None

    # ─────────────────────────────────────────
    # get_repo_map
    # ─────────────────────────────────────────

    async def get_repo_map(self, repo_id: str, ref: str = "main") -> str:
        """获取 GitHub 仓库目录树。"""
        url = f"{self._base_url}/repos/{repo_id}/git/trees/{ref}"
        session = await self._get_session()
        params = {"recursive": "true"}

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return "(无法获取仓库结构)"
            data = await resp.json()

        tree_items = data.get("tree", [])
        tree_lines: list[str] = []
        for item in tree_items[:200]:  # 限制条目
            path = item.get("path", "")
            item_type = item.get("type", "")
            prefix = "📁" if item_type == "tree" else "📄"
            tree_lines.append(f"{prefix} {path}")

        return "\n".join(tree_lines)
