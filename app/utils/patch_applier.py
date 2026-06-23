"""Search/Replace Block 应用器 - 精确 + 模糊替换工具类。

Blueprint V2 核心变更：
Fixer 不再输出极易出错的 Unified Diff，改为输出 Search/Replace Block。
本模块负责将这些 Block 应用到源文件上。

匹配策略（三级降级）：
1. 精确匹配：search 字符串在源文件中完全匹配
2. 去空白匹配：忽略行首尾空白后匹配
3. 模糊匹配：基于行的相似度匹配，找到最佳位置后替换

为什么不用 Unified Diff：
- Unified Diff 对上下文行数、空白字符极度敏感，LLM 经常生成错误格式
- Search/Replace Block 通过模糊匹配大幅提升容错性
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

            # 三级降级匹配策略
            content = result[file_path]
            matched = False

            # 策略 1：精确匹配
            if search in content:
                result[file_path] = content.replace(search, replace, 1)
                matched = True
                logger.info(
                    "Block #%d 已应用（精确匹配）: %s (search=%d chars → replace=%d chars)",
                    i, file_path, len(search), len(replace),
                )

            # 策略 2：去空白匹配（忽略行首尾空白）
            if not matched:
                normalized_result = self._try_normalized_match(content, search, replace)
                if normalized_result is not None:
                    result[file_path] = normalized_result
                    matched = True
                    logger.info(
                        "Block #%d 已应用（去空白匹配）: %s (search=%d chars → replace=%d chars)",
                        i, file_path, len(search), len(replace),
                    )

            # 策略 3：模糊匹配（基于核心行）
            if not matched:
                fuzzy_result = self._try_fuzzy_match(content, search, replace)
                if fuzzy_result is not None:
                    result[file_path] = fuzzy_result
                    matched = True
                    logger.warning(
                        "Block #%d 已应用（模糊匹配）: %s (search=%d chars → replace=%d chars)",
                        i, file_path, len(search), len(replace),
                    )

            if not matched:
                hint = self._find_closest_match(content, search)
                raise PatchApplyError(
                    file_path=file_path,
                    search=search,
                    message=f"Block #{i}: search 字符串在文件中未找到匹配（精确/去空白/模糊均失败）",
                    context=f"最接近的片段: {hint[:300]}" if hint else "无接近匹配",
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

    @staticmethod
    def _try_normalized_match(content: str, search: str, replace: str) -> str | None:
        """去空白匹配：忽略每行首尾空白后查找匹配。

        LLM 常见错误：多/少了一个空格、缩进差异。
        """
        def normalize_lines(text: str) -> list[str]:
            return [line.strip() for line in text.splitlines()]

        content_lines = content.splitlines()
        search_norm = normalize_lines(search)

        if not search_norm:
            return None

        # 逐行扫描，找连续匹配（归一化后）
        for i in range(len(content_lines) - len(search_norm) + 1):
            match = True
            for j, s_line in enumerate(search_norm):
                if content_lines[i + j].strip() != s_line:
                    match = False
                    break
            if match:
                # 找到匹配，执行替换
                replace_lines = replace.splitlines()
                new_lines = content_lines[:i] + replace_lines + content_lines[i + len(search_norm):]
                return "\n".join(new_lines)

        return None

    @staticmethod
    def _try_fuzzy_match(content: str, search: str, replace: str) -> str | None:
        """模糊行匹配：当 search 中部分行缺失时，尝试基于首尾行定位。

        LLM 常见错误：漏写了 search 块中的几行（如 import 语句）。
        策略：取 search 的首尾行作为锚点，在 content 中找到包含首尾行的区域。
        """
        search_lines = search.splitlines()
        if len(search_lines) < 2:
            return None

        # 取首尾行作为锚点（跳过空行）
        first_line = search_lines[0].strip()
        last_line = ""
        for line in reversed(search_lines):
            if line.strip():
                last_line = line.strip()
                break

        if not first_line or not last_line or first_line == last_line:
            return None

        content_lines = content.splitlines()

        # 找首行位置
        first_indices = [i for i, line in enumerate(content_lines) if line.strip() == first_line]
        if not first_indices:
            return None

        # 找尾行位置
        last_indices = [i for i, line in enumerate(content_lines) if line.strip() == last_line]
        if not last_indices:
            return None

        # 配对：首行在前，尾行在后，且中间距离合理
        for fi in first_indices:
            for li in last_indices:
                if li <= fi:
                    continue
                # 距离不能太远（search 行数的 3 倍以内）
                if li - fi > len(search_lines) * 3:
                    continue

                # 计算匹配度：search 中间行在 content 区间内出现的比例
                matched = 0
                for s_line in search_lines[1:-1]:
                    s_stripped = s_line.strip()
                    if not s_stripped:
                        continue
                    for ci in range(fi + 1, li):
                        if content_lines[ci].strip() == s_stripped:
                            matched += 1
                            break

                middle_lines = [l for l in search_lines[1:-1] if l.strip()]
                if not middle_lines:
                    # search 只有首尾两行，直接匹配
                    match_ratio = 1.0
                else:
                    match_ratio = matched / len(middle_lines)

                # 匹配度 >= 50% 认为成功
                if match_ratio >= 0.5:
                    replace_lines = replace.splitlines()
                    new_lines = content_lines[:fi] + replace_lines + content_lines[li + 1:]
                    return "\n".join(new_lines)

        return None

    @staticmethod
    def _try_normalized_match(content: str, search: str, replace: str) -> str | None:
        """去空白匹配：忽略每行首尾空白后查找匹配。

        LLM 常见错误：多/少了一个空格、缩进不同。
        """
        def normalize(text: str) -> str:
            return "\n".join(line.strip() for line in text.splitlines())

        norm_content = normalize(content)
        norm_search = normalize(search)

        if norm_search not in norm_content:
            return None

        # 在规范化文本中找到匹配位置
        norm_idx = norm_content.find(norm_search)

        # 回到原始文本中定位：按行对齐
        content_lines = content.splitlines(keepends=True)
        search_lines = search.splitlines()

        # 找到 search 第一行（strip 后）在 content 中的位置
        first_search_line = search_lines[0].strip()
        match_start_line = -1
        for line_idx, line in enumerate(content_lines):
            if line.strip() == first_search_line:
                # 验证后续行是否也匹配
                all_match = True
                for j, sline in enumerate(search_lines):
                    if line_idx + j >= len(content_lines):
                        all_match = False
                        break
                    if content_lines[line_idx + j].strip() != sline.strip():
                        all_match = False
                        break
                if all_match:
                    match_start_line = line_idx
                    break

        if match_start_line < 0:
            return None

        # 替换：用 replace 的行替换原始行
        search_count = len(search_lines)
        replace_lines = replace.splitlines(keepends=True)
        # 保持原始缩进
        indent = ""
        for c in content_lines[match_start_line]:
            if c in (" ", "\t"):
                indent += c
            else:
                break

        # 为 replace 行添加相同缩进
        adjusted_replace_lines = []
        for rline in replace_lines:
            stripped = rline.strip()
            if stripped:
                adjusted_replace_lines.append(indent + stripped + "\n")
            else:
                adjusted_replace_lines.append("\n")

        # 保留最后的换行符
        if replace_lines and not replace_lines[-1].endswith("\n"):
            last = adjusted_replace_lines[-1]
            if last.endswith("\n"):
                adjusted_replace_lines[-1] = last[:-1]

        new_lines = (
            content_lines[:match_start_line]
            + adjusted_replace_lines
            + content_lines[match_start_line + search_count:]
        )
        return "".join(new_lines)

    @staticmethod
    def _try_fuzzy_match(content: str, search: str, replace: str) -> str | None:
        """模糊行匹配：当 search 中部分行缺失时，尝试基于核心行定位。

        策略：
        1. 取 search 的首尾行作为锚点
        2. 在 content 中找到首行位置
        3. 验证尾行是否在合理范围内出现
        4. 如果匹配，替换整个区域
        """
        search_lines = [l.strip() for l in search.splitlines() if l.strip()]
        if len(search_lines) < 2:
            return None

        content_lines = content.splitlines(keepends=True)
        first_line = search_lines[0]
        last_line = search_lines[-1]

        # 找到首行在 content 中的位置
        first_indices = []
        for idx, line in enumerate(content_lines):
            if line.strip() == first_line:
                first_indices.append(idx)

        if not first_indices:
            return None

        for start_idx in first_indices:
            # 在首行之后的合理范围内找尾行
            # 允许 search_lines 长度的 2 倍范围内搜索
            max_search = min(len(content_lines), start_idx + len(search_lines) * 2)
            for end_idx in range(start_idx + 1, max_search):
                if content_lines[end_idx].strip() == last_line:
                    # 找到了首尾匹配，计算匹配质量
                    matched_count = 0
                    for sl in search_lines:
                        for ci in range(start_idx, end_idx + 1):
                            if content_lines[ci].strip() == sl:
                                matched_count += 1
                                break

                    match_ratio = matched_count / len(search_lines)
                    if match_ratio >= 0.7:  # 至少 70% 行匹配
                        # 执行替换
                        indent = ""
                        for c in content_lines[start_idx]:
                            if c in (" ", "\t"):
                                indent += c
                            else:
                                break

                        replace_lines = replace.splitlines(keepends=True)
                        adjusted_replace = []
                        for rline in replace_lines:
                            stripped = rline.strip()
                            if stripped:
                                adjusted_replace.append(indent + stripped + "\n")
                            else:
                                adjusted_replace.append("\n")

                        if replace_lines and not replace_lines[-1].endswith("\n"):
                            last = adjusted_replace[-1]
                            if last.endswith("\n"):
                                adjusted_replace[-1] = last[:-1]

                        new_lines = (
                            content_lines[:start_idx]
                            + adjusted_replace
                            + content_lines[end_idx + 1:]
                        )
                        return "".join(new_lines)

        return None
