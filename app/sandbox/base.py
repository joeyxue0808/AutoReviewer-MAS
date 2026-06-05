"""BaseSandboxEngine 抽象类 - Blueprint V2.0 沙盒引擎接口。

V2 变更：
- 支持 Search/Replace Block 替代 Unified Diff
- 命令白名单安全拦截（禁止任意 Shell 执行）
"""

import abc
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SandboxResult:
    """沙盒执行结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class BaseSandboxEngine(abc.ABC):
    """沙盒执行引擎抽象基类。"""

    @abc.abstractmethod
    async def run(
        self,
        image: str,
        command: str,
        source_files: Dict[str, str],
        search_replace_blocks: Optional[List[Dict[str, Any]]] = None,
        timeout: int = 300,
    ) -> SandboxResult:
        """在隔离沙盒中执行命令。

        Args:
            image: 容器镜像名（Docker 引擎）或忽略（Shell 引擎）
            command: 要执行的测试/Lint 命令（必须在白名单内）
            source_files: 源文件映射 (file_path -> content)
            search_replace_blocks: 可选的搜索/替换块列表，在执行命令前应用
            timeout: 超时秒数，默认 300（Blueprint 第 5 节）

        Returns:
            SandboxResult: 包含 exit_code、stdout、stderr、timed_out
        """
        ...

    @abc.abstractmethod
    async def cleanup(self) -> None:
        """清理沙盒资源（容器、临时文件等）。"""
        ...

    @staticmethod
    def apply_search_replace(
        source_files: Dict[str, str],
        blocks: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """将 Search/Replace Block 应用到源文件。

        对每个 block，精确搜索 source 中的代码片段并替换。
        如果搜索不到，跳过该 block 并记录警告。

        Args:
            source_files: 原始源文件映射
            blocks: SearchReplaceBlock 列表（dict 格式）

        Returns:
            修改后的源文件映射
        """
        import logging
        logger = logging.getLogger(__name__)

        result = dict(source_files)

        for block in blocks:
            file_path = block.get("file_path", "")
            search = block.get("search", "")
            replace = block.get("replace", "")

            if not file_path or not search:
                logger.warning("跳过无效 block: file_path=%s", file_path)
                continue

            if file_path not in result:
                logger.warning("文件不存在: %s，跳过 block", file_path)
                continue

            content = result[file_path]
            if search not in content:
                logger.warning(
                    "搜索文本未在 %s 中找到匹配，跳过 block",
                    file_path,
                )
                continue

            result[file_path] = content.replace(search, replace, 1)
            logger.info("已应用 Search/Replace: %s", file_path)

        return result
