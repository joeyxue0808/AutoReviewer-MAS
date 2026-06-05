"""read_file_context 工具 - 读取文件指定行范围的代码。

解决痛点：Diff 上下文截断导致的误判。
Reviewer 可主动调用此工具获取完整代码段，弥补 Diff 信息不足。
"""

import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
async def read_file_context(
    file_path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    repo_path: str = ".",
) -> str:
    """读取仓库中指定文件的代码内容。

    当你需要查看 Diff 中某段代码的完整上下文时调用此工具。
    可以指定行号范围，如果不指定则返回整个文件。

    Args:
        file_path: 文件的相对路径（如 "src/main.go"）
        start_line: 起始行号（从 1 开始，默认 1）
        end_line: 结束行号（包含该行，默认到文件末尾）
        repo_path: 仓库根目录路径（默认当前目录）

    Returns:
        指定行范围的代码内容（带行号）
    """
    import os

    full_path = os.path.join(repo_path, file_path)

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

    # 行号校验
    start_idx = max(0, start_line - 1)  # 转为 0-based index
    end_idx = total if end_line is None else min(end_line, total)

    if start_idx >= total:
        return f"错误: 起始行 {start_line} 超出文件总行数 {total}"

    selected = lines[start_idx:end_idx]

    # 带行号输出
    result_lines = []
    for i, line in enumerate(selected, start=start_idx + 1):
        result_lines.append(f"{i:4d} | {line.rstrip()}")

    content = "\n".join(result_lines)
    logger.debug("read_file_context: %s [%d-%d] (%d lines)", file_path, start_line, end_line or total, len(selected))

    return f"文件: {file_path} (第 {start_idx+1}-{end_idx} 行，共 {total} 行)\n```\n{content}\n```"
