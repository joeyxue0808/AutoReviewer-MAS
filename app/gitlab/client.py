"""GitLab API 异步封装 - 严格遵循 Blueprint 规范。

提供两个核心能力：
1. 拉取 MR Diff
2. 发表 MR 评论（审查结果）

使用 aiohttp 实现异步 HTTP 调用。
"""

import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from app.core.config import settings
from app.schemas.state import ReviewIssue

logger = logging.getLogger(__name__)


class GitLabClient:
    """GitLab REST API 异步客户端。"""

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
        """关闭 HTTP 会话。"""
        if self._session and not self._session.closed:
            await self._session.close()

    # ─────────────────────────────────────────
    # 核心方法 1：获取 MR Diff
    # ─────────────────────────────────────────

    async def get_mr_changes(
        self, project_id: int, mr_iid: int
    ) -> List[Dict[str, Any]]:
        """获取 MR 的文件变更列表。

        Args:
            project_id: GitLab 项目 ID
            mr_iid: MR 的 iid（项目级别 ID）

        Returns:
            变更列表，每项包含 old_path, new_path, diff 等字段
        """
        url = f"{self._base_url}/projects/{project_id}/merge_requests/{mr_iid}/changes"
        session = await self._get_session()

        async with session.get(url) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"获取 MR 变更失败: status={resp.status}, body={text}"
                )
            data = await resp.json()
            return data.get("changes", [])

    async def get_mr_diff_content(
        self, project_id: int, mr_iid: int
    ) -> str:
        """获取 MR 的完整 diff 内容（合并为单一字符串）。

        Args:
            project_id: GitLab 项目 ID
            mr_iid: MR iid

        Returns:
            合并后的 diff 文本
        """
        changes = await self.get_mr_changes(project_id, mr_iid)
        diffs = []
        for change in changes:
            diff = change.get("diff", "")
            new_path = change.get("new_path", "unknown")
            if diff:
                diffs.append(f"--- a/{new_path}\n+++ b/{new_path}\n{diff}")
        return "\n".join(diffs)

    # ─────────────────────────────────────────
    # 核心方法 2：发表 MR 评论
    # ─────────────────────────────────────────

    async def post_mr_comment(
        self,
        project_id: int,
        mr_iid: int,
        review_issues: List[ReviewIssue],
        test_logs: str = "",
        is_test_passed: bool = False,
    ) -> None:
        """向 MR 发表审查结果评论。

        格式化审查问题和测试日志为 Markdown，作为 MR Note 发布。

        Args:
            project_id: GitLab 项目 ID
            mr_iid: MR iid
            review_issues: 审查问题列表
            test_logs: 测试日志
            is_test_passed: 测试是否通过
        """
        body = self._format_comment(
            review_issues=review_issues,
            test_logs=test_logs,
            is_test_passed=is_test_passed,
        )

        url = f"{self._base_url}/projects/{project_id}/merge_requests/{mr_iid}/notes"
        session = await self._get_session()

        async with session.post(url, json={"body": body}) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise RuntimeError(
                    f"发表 MR 评论失败: status={resp.status}, body={text}"
                )
            logger.info(
                "已向 MR !%d (项目 %d) 发表审查评论",
                mr_iid,
                project_id,
            )

    def _format_comment(
        self,
        review_issues: List[ReviewIssue],
        test_logs: str = "",
        is_test_passed: bool = False,
    ) -> str:
        """将审查结果格式化为 Markdown 评论。"""
        parts = []

        # 标题
        status_icon = "✅" if is_test_passed else "❌"
        parts.append(f"## 🤖 AutoReviewer-MAS 审查报告 {status_icon}")
        parts.append("")

        # 审查问题
        if review_issues:
            parts.append(f"### 发现 {len(review_issues)} 个问题")
            parts.append("")
            parts.append("| # | 严重级别 | 文件 | 行号 | 描述 | 建议 |")
            parts.append("|---|---------|------|------|------|------|")

            severity_order = {"critical": 0, "warning": 1, "info": 2}
            sorted_issues = sorted(
                review_issues,
                key=lambda x: severity_order.get(x.severity, 3),
            )

            for i, issue in enumerate(sorted_issues, 1):
                severity_badge = {
                    "critical": "🔴 critical",
                    "warning": "🟡 warning",
                    "info": "🔵 info",
                }.get(issue.severity, issue.severity)

                parts.append(
                    f"| {i} | {severity_badge} | `{issue.file_path}` | "
                    f"{issue.line_number} | {issue.description} | {issue.suggestion} |"
                )
            parts.append("")
        else:
            parts.append("### ✅ 未发现问题")
            parts.append("")

        # 测试结果
        if test_logs:
            parts.append("### 🧪 测试结果")
            parts.append("")
            parts.append("```")
            parts.append(test_logs[:2000])  # 截断过长日志
            parts.append("```")
            parts.append("")

        parts.append("---")
        parts.append("*由 AutoReviewer-MAS 自动生成*")

        return "\n".join(parts)
