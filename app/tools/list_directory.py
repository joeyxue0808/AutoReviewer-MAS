"""list_directory 工具 - 探查目录结构。

解决痛点：Agent 需要快速理解未知模块的依赖关系和项目结构。
返回格式化的目录树，帮助 Agent 建立全局代码视野。
"""

import logging
import os
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 跳过的目录（无审查价值或体积过大）
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv",
    "dist", "build", "vendor", ".idea", ".vscode", ".tox",
    ".mypy_cache", ".pytest_cache", "target", "bin", "obj",
}


class ListDirectoryInput(BaseModel):
    """list_directory 的输入 Schema（防止参数幻觉）。"""

    dir_path: str = Field(
        default=".",
        description="要探查的目录相对路径（如 'src/auth'），默认为仓库根目录",
    )
    max_depth: int = Field(
        default=3,
        ge=1,
        le=5,
        description="最大递归深度（1-5，防止输出过大）",
    )
    repo_path: str = Field(
        default=".",
        description="仓库根目录路径",
    )


@tool(args_schema=ListDirectoryInput)
async def list_directory(
    dir_path: str = ".",
    max_depth: int = 3,
    repo_path: str = ".",
) -> str:
    """探查指定目录的文件树结构（只读）。

    当你遇到不熟悉的模块或包时，调用此工具快速了解其组织结构。
    返回目录树和关键文件列表。

    安全约束：此工具只读，不会修改任何文件。

    Args:
        dir_path: 要探查的目录相对路径（如 "src/auth"），默认为仓库根目录
        max_depth: 最大递归深度（默认 3 层，防止输出过大）
        repo_path: 仓库根目录路径（默认当前目录）

    Returns:
        格式化的目录树文本
    """
    full_dir = os.path.join(repo_path, dir_path)

    if not os.path.exists(full_dir):
        return f"错误: 目录不存在 - {dir_path}"

    if not os.path.isdir(full_dir):
        return f"错误: 路径不是目录 - {dir_path}"

    tree_lines = []
    file_count = 0
    dir_count = 0

    def _walk(current_path: str, prefix: str, depth: int):
        nonlocal file_count, dir_count

        if depth > max_depth:
            tree_lines.append(f"{prefix}... (达到最大深度 {max_depth})")
            return

        try:
            entries = sorted(os.listdir(current_path))
        except PermissionError:
            tree_lines.append(f"{prefix}⛔ (权限不足)")
            return

        # 分离目录和文件
        dirs = []
        files = []
        for entry in entries:
            full = os.path.join(current_path, entry)
            if os.path.isdir(full):
                if entry not in _SKIP_DIRS:
                    dirs.append(entry)
            else:
                files.append(entry)

        # 先显示目录
        for i, d in enumerate(dirs):
            is_last_dir = (i == len(dirs) - 1) and not files
            connector = "└── " if is_last_dir else "├── "
            tree_lines.append(f"{prefix}{connector}📁 {d}/")
            dir_count += 1

            extension = "    " if is_last_dir else "│   "
            _walk(os.path.join(current_path, d), prefix + extension, depth + 1)

        # 再显示文件
        for i, f in enumerate(files):
            is_last = i == len(files) - 1
            connector = "└── " if is_last else "├── "
            size = _format_size(os.path.getsize(os.path.join(current_path, f)))
            tree_lines.append(f"{prefix}{connector}📄 {f} ({size})")
            file_count += 1

    # 构建树
    display_path = dir_path if dir_path != "." else repo_path
    tree_lines.append(f"📁 {display_path}/")
    _walk(full_dir, "", 1)

    # 汇总信息
    summary = f"\n📊 统计: {dir_count} 个目录, {file_count} 个文件"

    return "\n".join(tree_lines) + summary


def _format_size(size_bytes: int) -> str:
    """格式化文件大小。"""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
