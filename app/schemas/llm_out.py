"""大模型强约束输出定义 - Blueprint V2.0 数据模型。

利用 LangChain 的 .with_structured_output() 方法，强制 LLM 返回 JSON。
V2 核心变更：Fixer 输出 SearchReplaceBlock 替代 Unified Diff。
"""

from typing import List

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# 共享模型
# ─────────────────────────────────────────────


class ReviewIssue(BaseModel):
    """审查问题（与 state.py 中的定义保持一致）。"""

    file_path: str = Field(description="问题所在的文件路径")
    line_number: int = Field(description="问题所在行号", default=0)
    severity: str = Field(description="严重级别: info / warning / critical")
    category: str = Field(default="general", description="问题分类: bug / style / security / performance / general")
    description: str = Field(description="问题描述")
    suggestion: str = Field(description="修复建议")

    @field_validator("line_number", mode="before")
    @classmethod
    def coerce_line_number(cls, v):
        """容忍 LLM 返回字符串行号（如 'N/A'、'unknown'），转为 0。"""
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            digits = "".join(c for c in v if c.isdigit())
            return int(digits) if digits else 0
        return 0


class SearchReplaceBlock(BaseModel):
    """搜索/替换块 - Fixer 输出格式（V2 核心变更）。

    替代 Unified Diff，通过精确的搜索/替换对实现代码修改。
    每个块对应一处独立的代码变更。
    """

    file_path: str = Field(description="目标文件路径")
    search: str = Field(description="要搜索的原始代码片段（必须精确匹配源文件中的内容）")
    replace: str = Field(description="替换后的新代码")
    start_line: int = Field(default=0, description="搜索区域起始行号（可选，辅助定位）")
    context_before: str = Field(default="", description="搜索块之前的上下文行（用于精确定位）")
    context_after: str = Field(default="", description="搜索块之后的上下文行（用于精确定位）")


# ─────────────────────────────────────────────
# Agent 输出模型
# ─────────────────────────────────────────────


class ReviewerOutput(BaseModel):
    """Reviewer Agent 的结构化输出。"""

    issues: List[ReviewIssue] = Field(
        default_factory=list,
        description="发现的代码缺陷列表",
    )
    is_approved: bool = Field(
        description="如果没有发现 critical 级别问题，则为 True",
    )


class FixerOutput(BaseModel):
    """Fixer Agent 的结构化输出 - V2 使用 Search/Replace Block。

    每个 block 是一个独立的搜索/替换对，
    可以精确地定位并修改源文件中的代码片段。
    """

    blocks: List[SearchReplaceBlock] = Field(
        description="搜索/替换块列表，每个块对应一处代码修复",
    )
    explanation: str = Field(
        default="",
        description="修复思路的简要说明",
    )
