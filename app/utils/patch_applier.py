"""Search/Replace Block 应用器 - 纯文本精确替换工具类。

Blueprint V2 核心变更：
Fixer 不再输出极易出错的 Unified Diff，改为输出 Search/Replace Block。
本模块负责将这些 Block 精确应用到源文件上。

使用逻辑：
1. FixerNode 通过 LLM 输出 List[SearchReplaceBlock]
2. PatchApplier 将每个 Block 的 search 精准定位到源文件中
3. 找到后替换为 replace 内容
4. 找不到则抛出 PatchApplyError 异常（而非静默失败）

为什么不用 Unified Diff：
- Unified Diff 对上下文行数、空白字符极度敏感，LLM 经常生成错误格式
- Search/Replace Block 只要求 search 内容与源文件精确匹配，容错性更高
- 失败时能给出明确的错误信息，便于 LLM 在重试时修正
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class PatchApplyError(Exception):
    """Patch 应用失败异常。

    当 SearchReplaceBlock 中的 search 字符串无法在源文件中找到时抛出。
    包含详细的诊断信息，便于 LLM 在重试时修正 search 内容。
    """

    def __init__(
        self,
        file_path: str,
        search: str,
        message: str,
        context: str = "",
    ) -> None:
        self.file_path = file_path
        self.search = search
        self.context = context
        super().__init__(
            f"PatchApplyError [{file_path}]: {message}\n"
            f"Search (前 200 字符): {search[:200]!r}\n"
            f"Context: {context[:300] if context else '(无)'}"
        )


@dataclass
class ApplyResult:
    """单个 Block 的应用结果。"""

    file_path: str
    success: bool
    search: str
    replace: str
    error: str = ""


class PatchApplier:
    """Search/Replace Block 纯文本替换应用器。

    核心逻辑：
    - 接收原始文件内容映射 (file_path -> content) 和 Block 列表
    - 对每个 Block，在对应文件中精确查找 search 字符串
    - 找到 → 替换为 replace（只替换第一次出现，避免误伤）
    - 找不到 → 抛出 PatchApplyError，包含诊断上下文
    """

    def apply(
        self,
        source_files: Dict[str, str],
        blocks: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """将 Search/Replace Block 列表应用到源文件。

        Args:
            source_files: 原始源文件映射 {file_path: content}
            blocks: SearchReplaceBlock 列表（dict 格式，来自 LLM 输出）

        Returns:
            修改后的源文件映射（新字典，不修改原始数据）

        Raises:
            PatchApplyError: 当 search 字符串无法在源文件中找到时
        """
        # 创建副本，避免修改原始数据
        result = dict(source_files)

        for i, block in enumerate(blocks):
            file_path = block.get("file_path", "")
            search = block.get("search_block", block.get("search", ""))
            replace = block.get("replace_block", block.get("replace", ""))

            # 校验 Block 字段完整性
            if not file_path:
                raise PatchApplyError(
                    file_path="(unknown)",
                    search=search,
                    message=f"Block #{i}: file_path 为空",
                )

            if not search:
                raise PatchApplyError(
                    file_path=file_path,
                    search="",
                    message=f"Block #{i}: search 为空字符串",
                )

            # 检查文件是否存在
            if file_path not in result:
                available = list(result.keys())
                raise PatchApplyError(
                    file_path=file_path,
                    search=search,
                    message=f"Block #{i}: 文件不存在于源文件映射中",
                    context=f"可用文件: {available[:10]}",
                )

            # 精确查找并替换
            content = result[file_path]
            if search not in content:
                # 提供诊断上下文：尝试模糊匹配
                hint = self._find_closest_match(content, search)
                raise PatchApplyError(
                    file_path=file_path,
                    search=search,
                    message=f"Block #{i}: search 字符串在文件中未找到精确匹配",
                    context=f"最接近的片段: {hint[:300]}" if hint else "无接近匹配",
                )

            # 执行替换（只替换第一次出现）
            result[file_path] = content.replace(search, replace, 1)
            logger.info(
                "Block #%d 已应用: %s (search=%d chars → replace=%d chars)",
                i, file_path, len(search), len(replace),
            )

        return result

    def try_apply(
        self,
        source_files: Dict[str, str],
        blocks: List[Dict[str, str]],
    ) -> tuple[Dict[str, str], List[ApplyResult]]:
        """尝试应用所有 Block，收集成功/失败结果（不抛异常）。

        适用于需要尽可能多地应用修复，而非遇到第一个失败就中止的场景。

        Args:
            source_files: 原始源文件映射
            blocks: SearchReplaceBlock 列表

        Returns:
            (修改后的源文件映射, 每个 Block 的应用结果列表)
        """
        result = dict(source_files)
        results: List[ApplyResult] = []

        for i, block in enumerate(blocks):
            file_path = block.get("file_path", "")
            search = block.get("search_block", block.get("search", ""))
            replace = block.get("replace_block", block.get("replace", ""))

            if not file_path or not search:
                results.append(ApplyResult(
                    file_path=file_path or "(unknown)",
                    success=False,
                    search=search,
                    replace=replace,
                    error=f"Block #{i}: file_path 或 search 为空",
                ))
                continue

            if file_path not in result:
                results.append(ApplyResult(
                    file_path=file_path,
                    success=False,
                    search=search,
                    replace=replace,
                    error=f"Block #{i}: 文件不存在",
                ))
                continue

            content = result[file_path]
            if search not in content:
                results.append(ApplyResult(
                    file_path=file_path,
                    success=False,
                    search=search,
                    replace=replace,
                    error=f"Block #{i}: search 未找到精确匹配",
                ))
                continue

            result[file_path] = content.replace(search, replace, 1)
            results.append(ApplyResult(
                file_path=file_path,
                success=True,
                search=search,
                replace=replace,
            ))

        return result, results

    @staticmethod
    def _find_closest_match(content: str, search: str) -> str:
        """尝试在 content 中找到与 search 最接近的片段（用于诊断）。

        策略：取 search 的第一行，在 content 中查找包含该行的区域。
        """
        if not search or not content:
            return ""

        # 取 search 的第一行作为锚点
        first_line = search.strip().split("\n")[0].strip()
        if not first_line or len(first_line) < 10:
            return ""

        idx = content.find(first_line)
        if idx == -1:
            return ""

        # 返回匹配位置周围的上下文
        start = max(0, idx - 50)
        end = min(len(content), idx + len(first_line) + 200)
        return content[start:end]
