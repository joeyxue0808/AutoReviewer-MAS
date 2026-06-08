"""空沙盒引擎 - 跳过所有测试执行。

当 Docker 和 Shell 都不可用时的最终降级方案。
始终返回 success=True，仅用于开发调试。
"""

import logging
from typing import Any, Dict, List, Optional

from app.sandbox.base import BaseSandboxEngine, SandboxResult

logger = logging.getLogger(__name__)


class NullSandbox(BaseSandboxEngine):
    """空沙盒引擎 — 不执行任何命令，始终返回成功。

    适用场景：
    - 本地开发环境无 Docker
    - 仅需 review + fix，不需要测试验证
    - 快速原型迭代
    """

    async def run(
        self,
        image: str,
        command: str,
        source_files: Dict[str, str],
        search_replace_blocks: Optional[List[Dict[str, Any]]] = None,
        timeout: int = 300,
    ) -> SandboxResult:
        """跳过执行，直接返回成功。"""
        logger.info("NullSandbox: 跳过执行 (image=%s, cmd=%s)", image, command)
        return SandboxResult(
            exit_code=0,
            stdout="[NullSandbox] 测试已跳过（沙箱未启用）",
            stderr="",
            timed_out=False,
        )

    async def cleanup(self) -> None:
        """无需清理。"""
        pass
