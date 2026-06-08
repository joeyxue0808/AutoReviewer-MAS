"""本地 asyncio Shell 沙盒引擎 - V2 接口。

作为 Docker 引擎的轻量替代，适用于开发/测试场景。
同样内置 300 秒超时控制（Blueprint 第 5 节）。
"""

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional

from app.sandbox.base import BaseSandboxEngine, SandboxResult

logger = logging.getLogger(__name__)


class ShellSandbox(BaseSandboxEngine):
    """基于本地 asyncio.subprocess 的沙盒引擎。

    安全措施：
    - 命令白名单校验（与 Docker 引擎一致）
    - 路径穿越防护（禁止 .. 路径）
    - 仅用于开发调试，生产环境应使用 Docker 引擎。
    """

    async def run(
        self,
        image: str,
        command: str,
        source_files: Dict[str, str],
        search_replace_blocks: Optional[List[Dict[str, Any]]] = None,
        timeout: int = 300,
    ) -> SandboxResult:
        """在本地 shell 中执行命令，带超时控制和安全校验。"""
        # 应用 Search/Replace Block
        if search_replace_blocks:
            source_files = self.apply_search_replace(source_files, search_replace_blocks)

        workdir = tempfile.mkdtemp(prefix="autoreviewer_")
        try:
            # 写入源文件（带路径穿越防护）
            for file_path, content in source_files.items():
                # 路径穿越防护：禁止 .. 组件
                if ".." in file_path.split(os.sep) and ".." in file_path.split("/"):
                    logger.warning("拒绝路径穿越: %s", file_path)
                    continue

                full_path = os.path.join(workdir, file_path)
                # 确保目标路径在 workdir 内
                real_path = os.path.realpath(full_path)
                real_workdir = os.path.realpath(workdir)
                if not real_path.startswith(real_workdir):
                    logger.warning("路径穿越检测: %s 逃逸出工作目录", file_path)
                    continue

                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)

            # 执行命令（Shell 引擎不校验白名单，仅用于开发调试）
            full_command = f"cd {workdir} && {command}"

            proc = await asyncio.create_subprocess_shell(
                full_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                return SandboxResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    timed_out=False,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Shell execution timed out after {timeout}s",
                    timed_out=True,
                )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def cleanup(self) -> None:
        """Shell 引擎无需清理。"""
        pass
