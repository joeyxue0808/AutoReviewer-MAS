"""read_file_context 工具 - 读取文件指定行范围的代码。

安全约束（Implementation Guide Phase 4 Task 4.1）：
- 只读操作，禁止任何 Write / Execute 逻辑
- 使用 Pydantic Schema 约束输入参数，防止参数幻觉
"""

import logging
import os
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ReadFileInput(BaseModel):
    """read_file_context 的输入 Schema（防止参数幻觉）。"""

    file_path: str = Field(
        description="文件的相对路径（如 'src/main.go'），禁止使用绝对路径或 .. 遍历",
    )
    start_line: int = Field(
        default=1,
        ge=1,
        description="起始行号（从 1 开始）",
    )
    end_line: Optional[int] = Field(
        default=None,
        ge=1,
        description="结束行号（包含该行，默认到文件末尾）",
    )
    repo_path: str = Field(
        default=".",
        description="仓库根目录路径（默认当前目录）",
    )


@tool(args_schema=ReadFileInput)
async def read_file_context(
    file_path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    repo_path: str = ".",
) -> str:
    """读取仓库中指定文件的代码内容（只读）。

    当你需要查看 Diff 中某段代码的完整上下文时调用此工具。
    可以指定行号范围，如果不指定则返回整个文件。

    安全约束：此工具只读，不会修改任何文件。
    """
    full_path = os.path.join(repo_path, file_path)

    # 安全校验：禁止路径遍历
    if ".." in file_path:
        return "错误: 禁止使用 .. 路径遍历"

    if not os.path.exists(full_path):
        return f"错误: 文件不存在 - {file_path}"

    if not os.path.isfile(full_path):
        return f"错误: 路径不是文件 - {file_path}"

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"错误: 读取文件失败 - {e}"

    total = len(lines)
    start_idx = max(0, start_line - 1)
    end_idx = total if end_line is None else min(end_line, total)

    if start_idx >= total:
        return f"错误: 起始行 {start_line} 超出文件总行数 {total}"

    selected = lines[start_idx:end_idx]

    result_lines = []
    for i, line in enumerate(selected, start=start_idx + 1):
        result_lines.append(f"{i:4d} | {line.rstrip()}")

    content = "\n".join(result_lines)
    logger.debug("read_file_context: %s [%d-%d] (%d lines)", file_path, start_line, end_line or total, len(selected))

    return f"文件: {file_path} (第 {start_idx+1}-{end_idx} 行，共 {total} 行)\n```\n{content}\n```"
