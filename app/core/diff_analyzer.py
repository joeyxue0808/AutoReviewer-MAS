"""Diff 预处理与语言检测 - Blueprint V2 第 2 节。

核心职责：
1. detect_languages: 通过解析 Diff 中被修改文件的路径，精确返回包含的语言列表
2. chunk_diff: 过滤自动生成文件 + 按文件路径拆分 Chunk，防止 Context Window 爆炸

防爆炸策略说明：
- LLM 的 Context Window 有 token 上限（通常 4k~128k）
- 一个大型 MR 可能包含数千行 Diff，远超 Context Window
- 解决方案：按文件路径拆分为多个 Chunk，每个 Chunk 只包含单个语言的 Diff
- 这样每个 Chunk 的 token 量可控，且语言上下文纯净，提升审查质量
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

from app.core.language_matrix import LANGUAGE_MATRIX, get_lang_by_suffix

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 自动生成文件黑名单（这些文件的 Diff 必须过滤掉）
# 过滤原因：这些文件通常是机器生成的，Diff 体积大且无审查价值
# ─────────────────────────────────────────────
_AUTO_GENERATED_PATTERNS: tuple[str, ...] = (
    "package-lock.json",   # npm 依赖锁定文件（可达数万行）
    "yarn.lock",           # yarn 依赖锁定文件
    "pnpm-lock.yaml",      # pnpm 依赖锁定文件
    "go.sum",              # Go 依赖校验文件
    "go.mod",              # Go 模块定义（通常无需审查）
    "Cargo.lock",          # Rust 依赖锁定文件
    "poetry.lock",         # Python Poetry 依赖锁定文件
    "Pipfile.lock",        # Python Pipenv 依赖锁定文件
    "composer.lock",       # PHP Composer 依赖锁定文件
    "Gemfile.lock",        # Ruby 依赖锁定文件
    ".min.js",             # 压缩后的 JavaScript
    ".min.css",            # 压缩后的 CSS
    ".map",                # Source Map 文件
    "dist/",               # 构建产物目录
    "build/",              # 构建产物目录
    "vendor/",             # 第三方依赖目录
    "node_modules/",       # Node.js 依赖目录
    "__pycache__/",        # Python 编译缓存
    ".git/",               # Git 内部文件
)

# Diff 中文件路径的正则匹配模式
# Unified Diff 格式：diff --git a/path/to/file b/path/to/file
_DIFF_FILE_PATTERN = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

# 单文件 Diff 块的最大行数阈值（超过则触发 Chunk 拆分）
_CHUNK_LINE_THRESHOLD = 500


@dataclass
class DiffChunk:
    """拆分后的 Diff 块。"""

    language: str  # 语言 key（如 "go", "python"）
    file_path: str  # 文件路径
    content: str  # 该文件的完整 Diff 内容
    line_count: int  # Diff 行数


class DiffAnalyzer:
    """Diff 预处理器。

    职责：
    1. 从原始 Diff 文本中检测涉及的编程语言
    2. 过滤掉自动生成文件的 Diff（防噪音 + 防爆炸）
    3. 按文件路径拆分为 Chunk，每个 Chunk 对应一个语言 + 一个文件
    """

    def __init__(self, line_threshold: int = _CHUNK_LINE_THRESHOLD) -> None:
        """
        Args:
            line_threshold: 单文件 Diff 超过此行数时触发告警（默认 500 行）
        """
        self._line_threshold = line_threshold

    # ─────────────────────────────────────────
    # 核心方法 1：语言检测
    # ─────────────────────────────────────────

    def detect_languages(self, diff_text: str) -> List[str]:
        """通过解析 Diff 中被修改文件的路径，精确返回涉及的语言列表。

        实现逻辑：
        1. 用正则提取 Diff 中所有 "diff --git a/... b/..." 行的文件路径
        2. 对每个文件路径提取后缀
        3. 通过 language_matrix 的反向索引查找对应语言
        4. 去重后返回

        Args:
            diff_text: 原始 Git Diff 文本

        Returns:
            检测到的语言 key 列表，如 ["go", "python"]
        """
        if not diff_text:
            return []

        detected: Set[str] = set()

        for match in _DIFF_FILE_PATTERN.finditer(diff_text):
            file_path = match.group(2)  # 取 b/ 后面的路径（新文件路径）

            # 过滤自动生成文件
            if self._is_auto_generated(file_path):
                continue

            # 提取后缀并查找语言
            suffix = self._extract_suffix(file_path)
            if suffix:
                lang = get_lang_by_suffix(suffix)
                if lang:
                    detected.add(lang)

        result = sorted(detected)
        logger.info("语言检测结果: %s (共 %d 种)", result, len(result))
        return result

    # ─────────────────────────────────────────
    # 核心方法 2：Diff Chunk 拆分（防爆炸）
    # ─────────────────────────────────────────

    def chunk_diff(self, diff_text: str) -> Dict[str, str]:
        """将原始 Diff 按语言拆分为多个 Chunk，防止 Context Window 爆炸。

        防爆炸策略：
        1. 【过滤】移除 package-lock.json、go.sum 等自动生成文件的 Diff
           - 这些文件通常数千行且无审查价值，是 Context Window 爆炸的主要元凶
        2. 【拆分】按文件路径切分 Diff，每个文件独立一个块
        3. 【聚合】将同一语言的所有文件 Diff 聚合为一个 Chunk
           - 好处：每个 Chunk 语言上下文纯净，LLM 审查更精准
        4. 【告警】单文件 Diff 超过阈值时记录警告

        Args:
            diff_text: 原始 Git Diff 文本

        Returns:
            Dict[language_key, chunk_content]，如:
            {
                "go": "diff --git a/main.go ...\ndiff --git a/handler.go ...",
                "python": "diff --git a/app.py ..."
            }
        """
        if not diff_text:
            return {}

        # Step 1: 按 "diff --git" 分割为单文件 Diff 块
        file_diffs = self._split_by_file(diff_text)

        # Step 2: 过滤 + 分类
        chunks: Dict[str, List[str]] = {}  # lang -> [diff1, diff2, ...]

        for file_path, file_diff in file_diffs:
            # 过滤自动生成文件
            if self._is_auto_generated(file_path):
                logger.debug("过滤自动生成文件: %s", file_path)
                continue

            # 检测语言
            suffix = self._extract_suffix(file_path)
            lang = get_lang_by_suffix(suffix) if suffix else None

            if not lang:
                logger.debug("无法识别语言，跳过: %s", file_path)
                continue

            # 检查单文件 Diff 行数
            line_count = file_diff.count("\n")
            if line_count > self._line_threshold:
                logger.warning(
                    "单文件 Diff 过大: %s (%d 行，阈值 %d)，可能消耗大量 token",
                    file_path, line_count, self._line_threshold,
                )

            chunks.setdefault(lang, []).append(file_diff)

        # Step 3: 聚合为最终结果
        result: Dict[str, str] = {}
        for lang, diffs in chunks.items():
            result[lang] = "\n".join(diffs)
            total_lines = sum(d.count("\n") for d in diffs)
            logger.info(
                "Chunk [%s]: %d 个文件, %d 行",
                lang, len(diffs), total_lines,
            )

        return result

    # ─────────────────────────────────────────
    # 辅助方法
    # ─────────────────────────────────────────

    def _split_by_file(self, diff_text: str) -> List[tuple[str, str]]:
        """将完整 Diff 按文件路径切分为 (file_path, file_diff) 列表。"""
        parts = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)

        result: list[tuple[str, str]] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue

            match = _DIFF_FILE_PATTERN.search(part)
            if match:
                file_path = match.group(2)
                result.append((file_path, part))
            else:
                # 无法识别文件路径的 Diff 片段，归入 "unknown"
                result.append(("__unknown__", part))

        return result

    def _is_auto_generated(self, file_path: str) -> bool:
        """判断文件是否为自动生成（应过滤掉）。"""
        for pattern in _AUTO_GENERATED_PATTERNS:
            if pattern in file_path:
                return True
        return False

    def _extract_suffix(self, file_path: str) -> str | None:
        """从文件路径中提取后缀（如 ".go", ".py"）。

        处理逻辑：
        - 取最后一个 "." 后的部分
        - 忽略无后缀的文件（如 Makefile, Dockerfile）
        """
        # 去除路径中的目录部分，只保留文件名
        filename = file_path.rsplit("/", 1)[-1]

        if "." not in filename:
            return None

        # 取最后一个 .xxx
        suffix = "." + filename.rsplit(".", 1)[-1]
        return suffix
