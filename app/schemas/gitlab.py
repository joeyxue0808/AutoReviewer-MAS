"""GitLab Webhook Payload 模型。

用于解析 GitLab Push/MR Webhook 发送的 JSON 数据。
"""

from typing import Optional

from pydantic import BaseModel, Field


class GitLabProject(BaseModel):
    """Webhook 中的项目信息。"""

    id: int
    name: str
    path_with_namespace: str
    web_url: str


class GitLabBranch(BaseModel):
    """Webhook 中的分支信息。"""

    name: str


class GitLabMRAttributes(BaseModel):
    """Webhook 中的 MR 属性。"""

    iid: int
    title: str
    source_branch: str
    target_branch: str
    action: str  # open, update, reopen 等
    state: str


class GitLabWebhookPayload(BaseModel):
    """GitLab Merge Request Webhook 完整 Payload。

    对应 GitLab Webhook 事件格式:
    https://docs.gitlab.com/ee/user/project/integrations/webhooks.html
    """

    object_kind: str = "merge_request"
    project: GitLabProject
    object_attributes: GitLabMRAttributes = Field(alias="object_attributes")
    ref: Optional[str] = None

    @property
    def mr_iid(self) -> int:
        return self.object_attributes.iid

    @property
    def project_id(self) -> int:
        return self.project.id

    @property
    def source_branch(self) -> str:
        return self.object_attributes.source_branch

    @property
    def target_branch(self) -> str:
        return self.object_attributes.target_branch
