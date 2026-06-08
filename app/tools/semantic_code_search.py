"""语义代码搜索 Tool - 基于向量检索的代码上下文召回。

为 Reviewer/Fixer Agent 提供语义搜索能力，
在预加载 diff 上下文之外，检索与变更相关的代码片段。
"""

import logging
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SemanticSearchInput(BaseModel):
    """语义搜索输入。"""
    query: str = Field(description="搜索查询（自然语言或代码片段）")
    top_k: int = Field(default=5, description="返回结果数量")


@tool("semantic_code_search", args_schema=SemanticSearchInput)
async def semantic_code_search(query: str, top_k: int = 5) -> str:
    """语义搜索代码库中与 query 最相关的代码片段。

    使用向量检索在已索引的代码库中查找语义相似的代码。
    适用于：
    - 查找与变更相关的实现代码
    - 查找调用链和依赖关系
    - 查找相似模式的现有代码

    Args:
        query: 搜索查询（可以是自然语言描述或代码片段）
        top_k: 返回最相关的结果数量，默认 5

    Returns:
        格式化的搜索结果，包含文件路径、行号和代码内容
    """
    try:
        from app.rag.indexer import RepoIndexer

        indexer = RepoIndexer(repo_path=".")
        results = await indexer.search(query, top_k=top_k)

        if not results:
            return "(无搜索结果，请先运行索引构建)"

        parts = [f"## 语义搜索结果 (query: {query[:50]}...)\n"]
        for i, r in enumerate(results, 1):
            parts.append(
                f"### {i}. {r.file_path}:{r.start_line}-{r.end_line} "
                f"(score: {r.score:.3f})\n"
                f"**Symbol**: {r.symbol_name}\n"
                f"```\n{r.content[:1000]}\n```\n"
            )

        return "\n".join(parts)

    except Exception as e:
        logger.warning("语义搜索失败: %s", e)
        return f"(语义搜索不可用: {e})"
