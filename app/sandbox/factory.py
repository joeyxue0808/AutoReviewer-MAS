"""自适应沙箱工厂 - 自动检测可用引擎并降级。

创建顺序：Docker → Shell → Null（跳过）
确保在任何环境下都能运行，不会因为缺少 Docker 而崩溃。
"""

import logging
from typing import Optional

from app.sandbox.base import BaseSandboxEngine

logger = logging.getLogger(__name__)


async def create_sandbox(
    preferred_engine: Optional[str] = None,
) -> BaseSandboxEngine:
    """自适应创建沙箱引擎。

    按优先级尝试：
    1. Docker（如配置指定且 daemon 可用）
    2. Shell（如配置允许）
    3. Null（最终降级，跳过测试）

    Args:
        preferred_engine: 优先使用的引擎名 ("docker" / "shell")，
                         为 None 时从 settings 读取

    Returns:
        可用的沙箱引擎实例
    """
    from app.core.config import settings

    engine_name = preferred_engine or settings.sandbox.default_engine

    # 尝试 Docker
    if engine_name == "docker":
        try:
            from app.sandbox.docker_engine import DockerSandbox
            sandbox = DockerSandbox()
            # 健康检查：尝试连接 Docker daemon
            import docker
            client = docker.from_env()
            client.ping()
            client.close()
            logger.info("Docker 沙箱已就绪")
            return sandbox
        except Exception as e:
            logger.warning("Docker 不可用 (%s)，尝试降级", e)

    # 尝试 Shell
    if engine_name in ("docker", "shell"):
        try:
            from app.sandbox.shell_engine import ShellSandbox
            logger.info("降级到 Shell 沙箱（开发模式）")
            return ShellSandbox()
        except Exception as e:
            logger.warning("Shell 沙箱不可用: %s", e)

    # 最终降级：Null
    from app.sandbox.null_engine import NullSandbox
    logger.warning("所有沙箱引擎不可用，降级为 NullSandbox（跳过测试）")
    return NullSandbox()
