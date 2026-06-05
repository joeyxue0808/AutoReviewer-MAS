"""GitHub Webhook Payload 模型。

用于解析 GitHub Pull Request Webhook 事件。
https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request
"""

from typing import Optional

from pydantic import BaseModel, Field


class GitHubRepository(BaseModel):
    """Webhook 中的仓库信息。"""

    id: int
    full_name: str  # "owner/repo"
    name: str


class GitHubPullRequest(BaseModel):
    """Webhook 中的 PR 信息。"""

    number: int
    title: str
    state: str  # "open", "closed"
    head: dict  # {"ref": "branch-name", "sha": "..."}
    base: dict  # {"ref": "main", "sha": "..."}

    @property
    def source_branch(self) -> str:
        return self.head.get("ref", "")

    @property
    def target_branch(self) -> str:
        return self.base.get("ref", "")


class GitHubWebhookPayload(BaseModel):
    """GitHub Pull Request Webhook Payload。

    对应 GitHub Webhook 事件格式:
    https://docs.github.com/en/webhooks/webhook-events-and-payloads
    """

    action: str  # "opened", "synchronize", "reopened", "closed"
    number: int
    pull_request: GitHubPullRequest
    repository: GitHubRepository

    @property
    def pr_number(self) -> int:
        return self.pull_request.number

    @property
    def repo_full_name(self) -> str:
        return self.repository.full_name
