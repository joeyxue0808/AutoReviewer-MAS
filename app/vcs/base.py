"""BaseVCSProvider 抽象类 - Blueprint V2.0 VCS 多态抽象。

统一 GitLab / GitHub / CLI 的接口契约。
所有 VCS 客户端必须实现此抽象类。
"""

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DiffResult:
    """获取 Diff 的统一返回结构。"""

    files: List[Dict[str, Any]]  # 每项含 file_path, diff, language, additions, deletions
    raw_diff: str  # 原始 diff 文本
    languages_detected: List[str]  # 检测到的语言列表


@dataclass
class CommentPayload:
    """发表评论的统一参数。"""

    body: str  # 评论正文（Markdown）
    file_path: Optional[str] = None  # 行内评论的目标文件
    line_number: Optional[int] = None  # 行内评论的目标行号


class BaseVCSProvider(abc.ABC):
    """VCS 平台抽象基类。

    定义了与代码托管平台交互的四个核心操作：
    - get_diff: 获取 PR/MR 的文件变更
    - post_comment: 发表审查评论
    - apply_commit: 提交代码修改
    - get_repo_map: 获取仓库结构概览
    """

    @abc.abstractmethod
    async def get_diff(self, repo_id: str, pr_id: str) -> DiffResult:
        """获取 PR/MR 的文件变更 diff。

        Args:
            repo_id: 仓库标识（GitLab project_id / GitHub owner/repo）
            pr_id: PR/MR 的 ID

        Returns:
            DiffResult: 包含文件变更列表、原始 diff、检测到的语言
        """
        ...

    @abc.abstractmethod
    async def post_comment(
        self,
        repo_id: str,
        pr_id: str,
        comments: List[CommentPayload],
    ) -> None:
        """向 PR/MR 发表评论。

        支持发表总体评论和行内评论。

        Args:
            repo_id: 仓库标识
            pr_id: PR/MR 的 ID
            comments: 评论列表
        """
        ...

    @abc.abstractmethod
    async def apply_commit(
        self,
        repo_id: str,
        pr_id: str,
        branch: str,
        message: str,
        changes: Dict[str, str],  # key: file_path, value: new_content
    ) -> str:
        """向 PR/MR 分支提交代码修改。

        Args:
            repo_id: 仓库标识
            pr_id: PR/MR 的 ID
            branch: 目标分支名
            message: commit message
            changes: 文件变更映射 (file_path -> new_content)

        Returns:
            str: commit SHA 或标识
        """
        ...

    @abc.abstractmethod
    async def get_repo_map(self, repo_id: str, ref: str = "main") -> str:
        """获取仓库结构概览（目录树 + 关键文件摘要）。

        用于为 LLM 提供项目上下文，帮助理解代码结构。

        Args:
            repo_id: 仓库标识
            ref: 分支或 commit ref

        Returns:
            str: 仓库结构文本描述
        """
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        """清理 HTTP 会话等资源。"""
        ...
