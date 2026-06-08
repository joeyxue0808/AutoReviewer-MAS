"""精准语言路由矩阵 - Blueprint V2 第 2 节。

定义 9 大技术栈的完整配置映射（后缀、Lint 命令、Test 命令、Docker 镜像）。
作为单一数据源，供 DiffAnalyzer、SandboxEngine、TesterNode 共同使用。

配置优先级：settings.yaml > 硬编码默认值
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class LanguageConfig:
    """单个语言的配置项。"""

    name: str  # 显示名称
    suffixes: tuple[str, ...]  # 文件特征后缀
    lint_command: str  # 静态扫描/Linter 白名单命令
    test_command: str  # 沙盒测试白名单命令
    image: str  # 沙盒基础镜像 (Docker)


# ─────────────────────────────────────────────
# 9 大技术栈路由矩阵（硬编码默认值，可被 settings.yaml 覆盖）
# ─────────────────────────────────────────────

_DEFAULT_MATRIX: Dict[str, LanguageConfig] = {
    "go": LanguageConfig(
        name="Go",
        suffixes=(".go",),
        lint_command="golangci-lint run",
        test_command="go test ./...",
        image="golang:1.23-alpine",
    ),
    "python": LanguageConfig(
        name="Python",
        suffixes=(".py",),
        lint_command="ruff check .",
        test_command="pytest",
        image="python:3.12-slim",
    ),
    "cpp": LanguageConfig(
        name="C++",
        suffixes=(".cpp", ".h", ".cc", ".hpp"),
        lint_command="cpplint",
        test_command="cmake . && make && ctest",
        image="gcc:latest",
    ),
    "java": LanguageConfig(
        name="Java",
        suffixes=(".java",),
        lint_command="checkstyle",
        test_command="mvn test",
        image="maven:3.9-eclipse-temurin-21",
    ),
    "vue": LanguageConfig(
        name="Vue",
        suffixes=(".vue",),
        lint_command="eslint --ext .vue src",
        test_command="vitest run",
        image="node:20-alpine",
    ),
    "javascript": LanguageConfig(
        name="Node.js",
        suffixes=(".js", ".cjs", ".mjs"),
        lint_command="eslint .",
        test_command="npm test",
        image="node:20-alpine",
    ),
    "typescript": LanguageConfig(
        name="TypeScript",
        suffixes=(".ts", ".tsx"),
        lint_command="tsc --noEmit",
        test_command="npm test",
        image="node:20-alpine",
    ),
    "flutter": LanguageConfig(
        name="Flutter",
        suffixes=(".dart",),
        lint_command="dart analyze",
        test_command="flutter test",
        image="ghcr.io/cirruslabs/flutter:stable",
    ),
    "csharp": LanguageConfig(
        name="Unity(C#)",
        suffixes=(".cs",),
        lint_command="dotnet format",
        test_command="dotnet test",
        image="mcr.microsoft.com/dotnet/sdk:8.0",
    ),
}


def _load_matrix_from_settings() -> Dict[str, LanguageConfig]:
    """从 settings.yaml 加载语言矩阵，合并硬编码默认值。"""
    try:
        from app.core.config import settings
        result = {}
        for lang_key, default_config in _DEFAULT_MATRIX.items():
            if lang_key in settings.sandbox.matrix:
                item = settings.sandbox.matrix[lang_key]
                result[lang_key] = LanguageConfig(
                    name=default_config.name,
                    suffixes=tuple(item.suffixes),
                    lint_command=item.lint_command,
                    test_command=item.test_command,
                    image=item.image,
                )
            else:
                result[lang_key] = default_config
        return result
    except Exception:
        return _DEFAULT_MATRIX


# 延迟加载的矩阵缓存
_MATRIX_CACHE: Optional[Dict[str, LanguageConfig]] = None


def _get_matrix() -> Dict[str, LanguageConfig]:
    """获取语言矩阵（优先 settings.yaml，回退硬编码默认值）。"""
    global _MATRIX_CACHE
    if _MATRIX_CACHE is None:
        _MATRIX_CACHE = _load_matrix_from_settings()
    return _MATRIX_CACHE


# 反向索引：后缀 → 语言 key（供 DiffAnalyzer 快速查找）
def _build_suffix_map() -> Dict[str, str]:
    result = {}
    for lang_key, config in _get_matrix().items():
        for suffix in config.suffixes:
            result[suffix] = lang_key
    return result


def get_lang_by_suffix(suffix: str) -> str | None:
    """根据文件后缀返回语言 key，未匹配返回 None。"""
    return _build_suffix_map().get(suffix)


def get_config(lang: str) -> LanguageConfig:
    """获取指定语言的配置。

    Raises:
        KeyError: 语言不在矩阵中
    """
    matrix = _get_matrix()
    if lang not in matrix:
        raise KeyError(f"不支持的语言: {lang}，可用: {list(matrix.keys())}")
    return matrix[lang]


def invalidate_cache() -> None:
    """清除矩阵缓存（用于配置热更新）。"""
    global _MATRIX_CACHE
    _MATRIX_CACHE = None
