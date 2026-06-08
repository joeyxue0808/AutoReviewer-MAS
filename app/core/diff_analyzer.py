"""Diff 预处理与语言检测 - 防上下文爆炸。

核心职责：
1. detect_languages: 通过解析 Diff 中被修改文件的路径，精确返回包含的语言列表
2. chunk_diff: 过滤自动生成文件 + 按 token 上限切片，防止 Context Window 爆炸

防爆炸策略：
- LLM Context Window 有 token 上限（通常 4k~128k）
- 大型 MR 可能包含数千行 Diff，远超上限
- 解决方案：按语言分组，每组内按 token 上限切分为多个 Chunk
- 每个 Chunk 语言上下文纯净，token 量可控
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Set

from app.core.language_matrix import get_lang_by_suffix, get_config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 自动生成文件黑名单
# ─────────────────────────────────────────────
_AUTO_GENERATED_PATTERNS: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "go.sum",
    "go.mod",
    "Cargo.lock",
    "poetry.lock",
    "Pipfile.lock",
    "composer.lock",
    "Gemfile.lock",
    ".min.js",
    ".min.css",
    ".map",
    "dist/",
    "build/",
    "vendor/",
    "node_modules/",
    "__pycache__/",
    ".git/",
)

# Diff 中文件路径的正则
_DIFF_FILE_PATTERN = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

# 粗略的 token 估算比率（1 token ≈ 4 字符，适用于中英混合代码）
_CHARS_PER_TOKEN = 4

# 默认 Chunk token 上限
DEFAULT_MAX_CHUNK_TOKENS = 8000


@dataclass
class DiffChunk:
    """拆分后的 Diff 块。"""

    chunk_id: str  # 唯一标识，如 "go_0", "python_1"
    language: str  # 语言 key
    content: str  # 该 Chunk 的 Diff 内容
    file_count: int  # 包含的文件数
    estimated_tokens: int  # 估算 token 数


class DiffAnalyzer:
    """Diff 预处理器。

    职责：
    1. 检测涉及的编程语言
    2. 过滤自动生成文件
    3. 按语言分组，每组内按 token 上限切分为多个 Chunk
    """

    def __init__(self, max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS) -> None:
        self._max_tokens = max_chunk_tokens

    # ─────────────────────────────────────────
    # 核心方法 1：语言检测
    # ─────────────────────────────────────────

    def detect_languages(self, diff_text: str) -> List[str]:
        """通过解析 Diff 中文件路径，精确返回涉及的语言列表。"""
        if not diff_text:
            return []

        detected: Set[str] = set()

        for match in _DIFF_FILE_PATTERN.finditer(diff_text):
            file_path = match.group(2)
            if self._is_auto_generated(file_path):
                continue
            suffix = self._extract_suffix(file_path)
            if suffix:
                lang = get_lang_by_suffix(suffix)
                if lang:
                    detected.add(lang)

        result = sorted(detected)
        logger.info("语言检测结果: %s (共 %d 种)", result, len(result))
        return result

    # ─────────────────────────────────────────
    # 核心方法 2：Diff Chunk 拆分（Token 上限）
    # ─────────────────────────────────────────

    def chunk_diff(self, diff_text: str) -> List[DiffChunk]:
        """将原始 Diff 按 token 上限切分为多个 Chunk。

        策略：
        1. 【过滤】移除自动生成文件的 Diff
        2. 【分组】按语言将文件分组
        3. 【切片】每组内按 token 上限切分：
           - 逐文件累加估算 token
           - 累加超过上限时，开启新 Chunk
        4. 【输出】返回 DiffChunk 列表，每项含 chunk_id、language、content

        Args:
            diff_text: 原始 Git Diff 文本

        Returns:
            DiffChunk 列表
        """
        if not diff_text:
            return []

        # Step 1: 按文件切分
        file_diffs = self._split_by_file(diff_text)

        # Step 2: 过滤 + 按语言分组
        by_lang: Dict[str, List[tuple[str, str]]] = {}  # lang -> [(file_path, diff)]

        for file_path, file_diff in file_diffs:
            if self._is_auto_generated(file_path):
                continue
            suffix = self._extract_suffix(file_path)
            lang = get_lang_by_suffix(suffix) if suffix else None
            if not lang:
                continue
            by_lang.setdefault(lang, []).append((file_path, file_diff))

        # Step 3: 按 token 上限切片
        chunks: List[DiffChunk] = []
        chunk_counters: Dict[str, int] = {}  # lang -> chunk 序号

        for lang, files in by_lang.items():
            current_parts: List[str] = []
            current_tokens = 0
            current_file_count = 0

            for file_path, file_diff in files:
                file_tokens = self._estimate_tokens(file_diff)

                # 如果单文件就超过上限，单独成 Chunk
                if file_tokens >= self._max_tokens:
                    # 先把已积累的 Chunk 存储
                    if current_parts:
                        chunks.append(self._make_chunk(
                            lang, chunk_counters, current_parts,
                            current_file_count, current_tokens,
                        ))
                        current_parts = []
                        current_tokens = 0
                        current_file_count = 0

                    # 超大文件单独成 Chunk
                    chunks.append(self._make_chunk(
                        lang, chunk_counters, [file_diff], 1, file_tokens,
                    ))
                    continue

                # 累加检查
                if current_tokens + file_tokens > self._max_tokens and current_parts:
                    # 当前 Chunk 已满，存储后开启新 Chunk
                    chunks.append(self._make_chunk(
                        lang, chunk_counters, current_parts,
                        current_file_count, current_tokens,
                    ))
                    current_parts = []
                    current_tokens = 0
                    current_file_count = 0

                current_parts.append(file_diff)
                current_tokens += file_tokens
                current_file_count += 1

            # 存储最后一个 Chunk
            if current_parts:
                chunks.append(self._make_chunk(
                    lang, chunk_counters, current_parts,
                    current_file_count, current_tokens,
                ))

        logger.info(
            "Diff 切片完成: %d 个 Chunk (max_tokens=%d)",
            len(chunks), self._max_tokens,
        )
        for c in chunks:
            logger.info(
                "  [%s] %s: %d 文件, ~%d tokens",
                c.chunk_id, c.language, c.file_count, c.estimated_tokens,
            )

        return chunks

    # ─────────────────────────────────────────
    # 辅助方法
    # ─────────────────────────────────────────

    def _make_chunk(
        self,
        lang: str,
        counters: Dict[str, int],
        parts: List[str],
        file_count: int,
        estimated_tokens: int,
    ) -> DiffChunk:
        """构建 DiffChunk 对象。"""
        idx = counters.get(lang, 0)
        counters[lang] = idx + 1
        return DiffChunk(
            chunk_id=f"{lang}_{idx}",
            language=lang,
            content="\n".join(parts),
            file_count=file_count,
            estimated_tokens=estimated_tokens,
        )

    def _estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数（区分 ASCII 和 CJK 字符）。

        ASCII 字符约 4 字符/token，CJK 字符约 1.5 字符/token。
        """
        ascii_chars = 0
        cjk_chars = 0
        other_chars = 0
        for c in text:
            cp = ord(c)
            if cp < 128:
                ascii_chars += 1
            elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
                cjk_chars += 1
            else:
                other_chars += 1
        return ascii_chars // 4 + int(cjk_chars / 1.5) + other_chars // 3

    def _split_by_file(self, diff_text: str) -> List[tuple[str, str]]:
        """将完整 Diff 按文件路径切分。"""
        parts = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
        result: list[tuple[str, str]] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            match = _DIFF_FILE_PATTERN.search(part)
            if match:
                result.append((match.group(2), part))
            else:
                result.append(("__unknown__", part))
        return result

    def _is_auto_generated(self, file_path: str) -> bool:
        """判断文件是否为自动生成。"""
        return any(p in file_path for p in _AUTO_GENERATED_PATTERNS)

    def _extract_suffix(self, file_path: str) -> str | None:
        """从文件路径提取后缀。"""
        filename = file_path.rsplit("/", 1)[-1]
        if "." not in filename:
            return None
        return "." + filename.rsplit(".", 1)[-1]
