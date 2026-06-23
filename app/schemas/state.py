"""LangGraph 状态机定义 - Blueprint V3.0 数据模型。

V2 变更点：
- 支持多 VCS 平台 (gitlab / github / cli)
- 按语言拆分 diff_chunks，防止上下文爆炸
- Fixer 输出 SearchReplaceBlock 替代 Unified Diff
- 增加 trigger_type 区分触发来源

V3.0 变更点：
- 新增 error_type / last_node 字段，支持错误恢复节点路由
- 新增 error_count 用于连续错误计数和降级判断
"""

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# 审查问题模型
# ─────────────────────────────────────────────


class ReviewIssue(BaseModel):
    """单个代码审查问题。"""

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
    """LangGraph 全局状态定义 - V3.0 增强版（支持多轮审查）。

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
    # Annotated[list, operator.add] 允许 Send API 并发多个 reviewer_node 同时写入，
    # 各自的结果会自动合并（列表拼接），而非冲突报错
    review_issues: Annotated[List[Dict[str, Any]], operator.add]  # Reviewer Agent 发现的问题

    # ── Fixer 输出（Search/Replace 格式）──
    search_replace_blocks: List[Dict[str, Any]]  # Fixer 输出的搜索/替换块

    # ── 测试结果 ──
    test_logs: str  # Tester 沙盒执行后的日志
    is_test_passed: bool  # 测试是否通过
    retry_count: int  # Fixer <-> Tester 的循环重试次数
    error_count: int  # 连续错误计数（429 等，用于防止死循环）

    # ── 错误恢复 (V3.0) ──
    error_type: str  # 错误类型: "429" / "timeout" / "connection" / ""
    last_node: str  # 出错的节点名（用于 error_recovery 路由回原节点）

    # ── 多轮审查控制 (V4.0) ──
    current_round: int  # 当前轮次（从 0 开始）
    max_rounds: int  # 最大轮次限制（默认 3）
    round_issues: Annotated[List[Dict[str, Any]], operator.add]  # 每轮发现的问题

    # ── 用户干预相关 ──
    user_input_queue: Any  # 异步队列，用于接收用户输入
    user_instructions: str  # 当前有效的用户指令
    user_decisions: Dict[str, bool]  # 用户对问题的决策
    pending_user_approval: bool  # 是否在等待用户批准
    user_approval_result: Optional[bool]  # 用户批准结果

    # ── 多轮结果追踪 ──
    fixed_issues: Annotated[List[Dict[str, Any]], operator.add]  # 已修复的问题
    remaining_issues: List[Dict[str, Any]]  # 剩余问题
    round_reports: List[Dict[str, Any]]  # 每轮的报告
