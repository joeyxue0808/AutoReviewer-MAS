"""VCS 平台抽象层。"""

from app.vcs.base import BaseVCSProvider, CommentPayload, DiffResult

__all__ = ["BaseVCSProvider", "CommentPayload", "DiffResult"]
