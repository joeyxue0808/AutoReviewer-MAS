"""精准语言路由矩阵 - Blueprint V2 第 2 节。

定义 9 大技术栈的完整配置映射（后缀、Lint 命令、Test 命令、Docker 镜像）。
作为常量字典维护，供 DiffAnalyzer 和 SandboxEngine 共同使用。
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class LanguageConfig:
    """单个语言的配置项。"""

    name: str  # 显示名称
    suffixes: tuple[str, ...]  # 文件特征后缀
    lint_command: str  # 静态扫描/Linter 白名单命令
    test_command: str  # 沙盒测试白名单命令
    image: str  # 沙盒基础镜像 (Docker)


# ─────────────────────────────────────────────
# 9 大技术栈路由矩阵（严格遵循 Blueprint V2 第 2 节表格）
# ─────────────────────────────────────────────

LANGUAGE_MATRIX: Dict[str, LanguageConfig] = {
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

# 反向索引：后缀 → 语言 key（供 DiffAnalyzer 快速查找）
_SUFFIX_TO_LANG: Dict[str, str] = {}
for _lang_key, _config in LANGUAGE_MATRIX.items():
    for _suffix in _config.suffixes:
        _SUFFIX_TO_LANG[_suffix] = _lang_key


def get_lang_by_suffix(suffix: str) -> str | None:
    """根据文件后缀返回语言 key，未匹配返回 None。"""
    return _SUFFIX_TO_LANG.get(suffix)


def get_config(lang: str) -> LanguageConfig:
    """获取指定语言的配置。

    Raises:
        KeyError: 语言不在矩阵中
    """
    if lang not in LANGUAGE_MATRIX:
        raise KeyError(f"不支持的语言: {lang}，可用: {list(LANGUAGE_MATRIX.keys())}")
    return LANGUAGE_MATRIX[lang]
