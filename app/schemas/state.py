"""LangGraph 状态机定义 - Blueprint V2.0 数据模型。

V2 变更点：
- 支持多 VCS 平台 (gitlab / github / cli)
- 按语言拆分 diff_chunks，防止上下文爆炸
- Fixer 输出 SearchReplaceBlock 替代 Unified Diff
- 增加 trigger_type 区分触发来源
"""

from typing import Any, Dict, List, TypedDict

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────
# 审查问题模型
# ─────────────────────────────────────────────


class ReviewIssue(BaseModel):
    """单个代码审查问题。"""

    file_path: str = Field(description="问题所在的文件路径")
    line_number: int = Field(description="问题所在行号")
    severity: str = Field(description="严重级别: info / warning / critical")
    category: str = Field(default="general", description="问题分类: bug / style / security / performance / general")
    description: str = Field(description="问题描述")
    suggestion: str = Field(description="修复建议")


# ─────────────────────────────────────────────
# Search/Replace Block（替代 Unified Diff）
# ─────────────────────────────────────────────


class SearchReplaceBlock(BaseModel):
    """搜索/替换块 - Blueprint V2 核心变更。

    Fixer 节点不再输出极易出错的 Unified Diff，
    改为输出精确的搜索/替换块，大幅提升代码修改成功率。
    """

    file_path: str = Field(description="目标文件路径")
    search: str = Field(description="要搜索的原始代码片段（必须精确匹配）")
    replace: str = Field(description="替换后的新代码")
    start_line: int = Field(default=0, description="搜索区域起始行号（可选，辅助定位）")
    context_before: str = Field(default="", description="搜索块之前的上下文行（用于精确定位）")
    context_after: str = Field(default="", description="搜索块之后的上下文行（用于精确定位）")


# ─────────────────────────────────────────────
# LangGraph 全局状态
# ─────────────────────────────────────────────


class ReviewState(TypedDict):
    """LangGraph 全局状态定义 - V2 增强版。

    所有节点通过读写此 TypedDict 进行数据流转。
    """

    # ── 来源标识 ──
    vcs_provider: str  # "gitlab", "github", "cli"
    pr_id: str  # Pull Request / Merge Request ID（字符串，兼容不同平台）
    trigger_type: str  # "webhook_pr", "webhook_comment", "cli"
    repo_id: str  # VCS 仓库 ID（用于 API 调用）

    # ── 仓库上下文 ──
    repo_context: str  # 提取的 Repo-Map（目录结构与全局引用关系）

    # ── Diff 按语言拆分 ──
    diff_chunks: Dict[str, str]  # key: chunk_id (如 "python_0"), value: diff 内容
    detected_languages: List[str]  # 识别出的技术栈列表，如 ["go", "vue"]

    # ── 审查结果 ──
    review_issues: List[Dict[str, Any]]  # Reviewer Agent 发现的问题

    # ── Fixer 输出（Search/Replace 格式）──
    search_replace_blocks: List[Dict[str, Any]]  # Fixer 输出的搜索/替换块

    # ── 测试结果 ──
    test_logs: str  # Tester 沙盒执行后的日志
    is_test_passed: bool  # 测试是否通过
    retry_count: int  # Fixer <-> Tester 的循环重试次数
    error_count: int  # 连续错误计数（429 等，用于防止死循环）
