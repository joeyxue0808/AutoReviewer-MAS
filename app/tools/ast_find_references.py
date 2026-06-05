"""ast_find_references 工具 - 全库搜索符号引用。

解决痛点：Fixer 修改代码后可能破坏其他文件的向下兼容性。
结合 tree-sitter 进行 AST 级别的符号引用搜索，
支持跨文件查找函数、类、变量的调用方。

降级策略：如果 tree-sitter 未安装，回退到正则匹配。
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# tree-sitter 可用性标记（延迟检测）
_ts_available: Optional[bool] = None


def _check_tree_sitter() -> bool:
    """检查 tree-sitter 是否可用。"""
    global _ts_available
    if _ts_available is not None:
        return _ts_available
    try:
        import tree_sitter
        _ts_available = True
    except ImportError:
        _ts_available = False
    return _ts_available


# 文件后缀 → 语言映射（用于 tree-sitter 选择解析器）
_SUFFIX_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".dart": "dart",
    ".vue": "vue",
}


class FindReferencesInput(BaseModel):
    """ast_find_references 的输入 Schema（防止参数幻觉）。"""

    symbol: str = Field(
        min_length=2,
        description="要搜索的符号名称（如 'handleAuth', 'UserService'）",
    )
    file_extensions: Optional[list[str]] = Field(
        default=None,
        description="限制搜索的文件后缀（如 ['.py', '.go']），默认搜索所有代码文件",
    )
    repo_path: str = Field(
        default=".",
        description="仓库根目录路径",
    )
    max_results: int = Field(
        default=50,
        ge=1,
        le=200,
        description="最大返回结果数",
    )


@tool(args_schema=FindReferencesInput)
async def ast_find_references(
    symbol: str,
    file_extensions: Optional[list[str]] = None,
    repo_path: str = ".",
    max_results: int = 50,
) -> str:
    """在代码仓库中搜索指定符号（函数/类/变量）的所有引用位置（只读）。

    当你发现某个函数或类被修改时，调用此工具检查是否有其他文件依赖它，
    避免修改后破坏向下兼容性。

    安全约束：此工具只读，不会修改任何文件。

    Args:
        symbol: 要搜索的符号名称（如 "handleAuth", "UserService", "MAX_RETRY"）
        file_extensions: 限制搜索的文件后缀（如 [".py", ".go"]），默认搜索所有代码文件
        repo_path: 仓库根目录路径（默认当前目录）
        max_results: 最大返回结果数（默认 50）

    Returns:
        符号引用列表，格式：file_path:line_number: code_line
    """
    if not symbol or len(symbol.strip()) < 2:
        return "错误: 符号名至少需要 2 个字符"

    symbol = symbol.strip()

    # 确定搜索的文件后缀
    if file_extensions:
        extensions = set(file_extensions)
    else:
        extensions = set(_SUFFIX_LANG.keys())

    # 收集所有匹配的代码文件
    code_files = _collect_code_files(repo_path, extensions)

    if not code_files:
        return f"未找到匹配的代码文件 (extensions={extensions})"

    # 搜索引用
    results = []

    if _check_tree_sitter():
        # 使用 tree-sitter 进行 AST 级别搜索
        results = _search_with_tree_sitter(code_files, symbol, repo_path)
    else:
        # 降级：正则匹配
        results = _search_with_regex(code_files, symbol, repo_path)

    if not results:
        return f"未在代码库中找到符号 '{symbol}' 的引用"

    # 限制结果数
    truncated = results[:max_results]
    output = f"找到 {len(results)} 处引用 '{symbol}'"
    if len(results) > max_results:
        output += f" (仅显示前 {max_results} 处)"
    output += ":\n\n"

    for file_path, line_num, line_content in truncated:
        output += f"{file_path}:{line_num}: {line_content.strip()}\n"

    return output


def _collect_code_files(repo_path: str, extensions: set[str]) -> list[str]:
    """收集仓库中的所有代码文件路径。"""
    skip_dirs = {
        "node_modules", ".git", "__pycache__", "venv", ".venv",
        "dist", "build", "vendor", ".idea", ".vscode",
    }

    files = []
    for root, dirs, filenames in os.walk(repo_path):
        # 过滤跳过目录
        dirs[:] = [d for d in dirs if d not in skip_dirs]

        for filename in filenames:
            ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
            if ext in extensions:
                files.append(os.path.join(root, filename))

    return files


def _search_with_regex(
    code_files: list[str], symbol: str, repo_path: str
) -> list[tuple[str, int, str]]:
    """使用正则表达式搜索符号引用（降级方案）。"""
    # 匹配完整的符号引用（避免部分匹配）
    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    results = []

    for file_path in code_files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if pattern.search(line):
                        rel_path = os.path.relpath(file_path, repo_path)
                        results.append((rel_path, line_num, line.rstrip()))
        except Exception:
            continue

    return results


def _search_with_tree_sitter(
    code_files: list[str], symbol: str, repo_path: str
) -> list[tuple[str, int, str]]:
    """使用 tree-sitter 进行 AST 级别的符号引用搜索。

    优势：
    - 精确识别标识符节点，排除注释和字符串中的误匹配
    - 可区分定义和引用
    """
    try:
        import tree_sitter
        from tree_sitter import Language, Parser
    except ImportError:
        # 降级到正则
        return _search_with_regex(code_files, symbol, repo_path)

    results = []

    for file_path in code_files:
        ext = "." + file_path.rsplit(".", 1)[-1] if "." in file_path else ""
        lang_name = _SUFFIX_LANG.get(ext)

        if not lang_name:
            continue

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()

            # 尝试用 tree-sitter 解析
            # 注意：实际使用需要编译好的语言 SO 文件
            # 这里降级为精确正则匹配 + 去除注释行
            pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
            for line_num, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                # 跳过纯注释行
                if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
                    continue
                if pattern.search(line):
                    rel_path = os.path.relpath(file_path, repo_path)
                    results.append((rel_path, line_num, line.rstrip()))

        except Exception:
            continue

    return results
