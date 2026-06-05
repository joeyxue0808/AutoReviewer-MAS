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

    注意：安全性低于 Docker 引擎，仅用于开发调试。
    """

    async def run(
        self,
        image: str,
        command: str,
        source_files: Dict[str, str],
        search_replace_blocks: Optional[List[Dict[str, Any]]] = None,
        timeout: int = 300,
    ) -> SandboxResult:
        """在本地 shell 中执行命令，带超时控制。"""
        # 应用 Search/Replace Block
        if search_replace_blocks:
            source_files = self.apply_search_replace(source_files, search_replace_blocks)

        workdir = tempfile.mkdtemp(prefix="autoreviewer_")
        try:
            # 写入源文件
            for file_path, content in source_files.items():
                full_path = os.path.join(workdir, file_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)

            # 执行命令
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
