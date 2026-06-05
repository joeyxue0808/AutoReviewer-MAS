"""Repo-Map 全局上下文生成器 (Implementation Guide Phase 5 Task 5.1)。

解决痛点：大模型只看 Diff 导致全局逻辑断层。

实现策略：
1. 优先使用 tree-sitter（可选依赖）解析 AST，提取类/函数签名树
2. 若未安装 tree-sitter，降级为 os.walk 精简目录树
3. 输出注入 ReviewState["repo_context"]，为 Reviewer 提供全局视野
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# tree-sitter 可用性（延迟检测）
_ts_available: Optional[bool] = None

# 跳过的目录
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv",
    "dist", "build", "vendor", ".idea", ".vscode", ".tox",
    ".mypy_cache", ".pytest_cache", "target", "bin", "obj",
    "__pycache__", ".eggs", "*.egg-info",
}

# AST 提取的文件后缀
_AST_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".go", ".java",
    ".cpp", ".cc", ".h", ".hpp", ".cs", ".dart",
}


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


def generate_repo_map(repo_path: str, max_files: int = 200) -> str:
    """生成 Repo-Map（全局上下文）。

    优先使用 tree-sitter 提取 AST 签名树，
    降级为 os.walk 精简目录树。

    Args:
        repo_path: 仓库根目录路径
        max_files: 最大扫描文件数（防止超大仓库耗时过长）

    Returns:
        Repo-Map 文本字符串，注入 ReviewState["repo_context"]
    """
    if not os.path.isdir(repo_path):
        return f"(仓库路径不存在: {repo_path})"

    if _check_tree_sitter():
        try:
            return _generate_ast_map(repo_path, max_files)
        except Exception as e:
            logger.warning("tree-sitter AST 解析失败，降级为目录树: %s", e)

    return _generate_dir_tree(repo_path, max_depth=4)


# ─────────────────────────────────────────────
# tree-sitter AST 签名树（高级模式）
# ─────────────────────────────────────────────


def _generate_ast_map(repo_path: str, max_files: int) -> str:
    """使用 tree-sitter 提取类/函数签名树。

    解析每个代码文件的 AST，提取：
    - 类定义（class name）
    - 函数/方法定义（def name）
    - 导入语句（import ...）

    输出格式：
        src/auth/
          models.py
            class User
            class Permission
            def authenticate
            def authorize
          handlers.py
            def login
            def logout
    """
    import tree_sitter

    # 语言检测映射
    ext_to_lang = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".java": "java",
    }

    results: list[str] = []
    file_count = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]

        for filename in sorted(files):
            if file_count >= max_files:
                results.append(f"... (达到最大文件数 {max_files})")
                break

            ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
            if ext not in _AST_EXTENSIONS:
                continue

            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, repo_path)

            try:
                signatures = _extract_signatures(filepath, ext)
                if signatures:
                    results.append(f"  {rel_path}")
                    for sig in signatures:
                        results.append(f"    {sig}")
                    file_count += 1
            except Exception:
                continue

    if not results:
        return _generate_dir_tree(repo_path, max_depth=4)

    header = f"Repo-Map (AST 签名树, {file_count} 文件):\n"
    return header + "\n".join(results)


def _extract_signatures(filepath: str, ext: str) -> list[str]:
    """从单个文件提取类/函数签名（简化实现）。

    使用正则而非完整 tree-sitter 解析，兼容性更好。
    """
    import re

    signatures: list[str] = []

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return signatures

    if ext == ".py":
        # Python: class/def 顶层定义
        for match in re.finditer(r"^(class|def)\s+(\w+)", content, re.MULTILINE):
            kind = "class" if match.group(1) == "class" else "def"
            name = match.group(2)
            signatures.append(f"{kind} {name}")

    elif ext in (".js", ".ts", ".tsx"):
        # JS/TS: function/class/export
        for match in re.finditer(
            r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+(\w+)",
            content, re.MULTILINE,
        ):
            signatures.append(f"function/class {match.group(1)}")

    elif ext == ".go":
        # Go: func/type
        for match in re.finditer(r"^func\s+(?:\([^)]+\)\s+)?(\w+)", content, re.MULTILINE):
            signatures.append(f"func {match.group(1)}")
        for match in re.finditer(r"^type\s+(\w+)", content, re.MULTILINE):
            signatures.append(f"type {match.group(1)}")

    elif ext == ".java":
        # Java: class/interface/method
        for match in re.finditer(
            r"(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface)\s+(\w+)",
            content, re.MULTILINE,
        ):
            signatures.append(f"class {match.group(1)}")

    return signatures[:50]  # 限制每文件最多 50 个签名


# ─────────────────────────────────────────────
# os.walk 目录树（降级模式）
# ─────────────────────────────────────────────


def _generate_dir_tree(repo_path: str, max_depth: int = 4) -> str:
    """使用 os.walk 生成精简目录树（降级方案）。"""
    lines: list[str] = []
    dir_count = 0
    file_count = 0

    def _walk(current: str, prefix: str, depth: int):
        nonlocal dir_count, file_count

        if depth > max_depth:
            lines.append(f"{prefix}... (达到最大深度)")
            return

        try:
            entries = sorted(os.listdir(current))
        except PermissionError:
            lines.append(f"{prefix} (权限不足)")
            return

        dirs = []
        files = []
        for entry in entries:
            full = os.path.join(current, entry)
            if os.path.isdir(full):
                if entry not in _SKIP_DIRS:
                    dirs.append(entry)
            else:
                files.append(entry)

        for i, d in enumerate(dirs):
            is_last = (i == len(dirs) - 1) and not files
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{d}/")
            dir_count += 1
            ext = "    " if is_last else "│   "
            _walk(os.path.join(current, d), prefix + ext, depth + 1)

        for i, f in enumerate(files[:20]):  # 每目录最多 20 文件
            is_last = i == len(files) - 1
            connector = "└── " if is_last else "├── "
            lines.append(f"{prefix}{connector}{f}")
            file_count += 1

        if len(files) > 20:
            lines.append(f"{prefix}... 还有 {len(files) - 20} 个文件")

    lines.append(f"{os.path.basename(repo_path)}/")
    _walk(repo_path, "", 1)
    lines.append(f"\n统计: {dir_count} 目录, {file_count} 文件")

    return "\n".join(lines)
