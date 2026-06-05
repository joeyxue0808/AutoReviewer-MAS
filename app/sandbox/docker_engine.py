"""Docker 沙盒引擎 - Blueprint V2.0 规范。

V2 变更：
- 使用 Search/Replace Block 替代 Unified Diff patch
- 命令白名单安全拦截（禁止任意 Shell 执行）
- asyncio 包装 docker-py 调用，300 秒超时防护
"""

import asyncio
import io
import json
import logging
import tarfile
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, ContainerError, ImageNotFound

from app.sandbox.base import BaseSandboxEngine, SandboxResult

logger = logging.getLogger(__name__)

# 命令白名单 - 仅允许 LanguageMatrix 中定义的标准命令
# 所有沙盒执行的命令必须以白名单中的前缀开头
ALLOWED_COMMAND_PREFIXES: set[str] = {
    "go test",
    "go build",
    "golangci-lint",
    "pytest",
    "flake8",
    "ruff",
    "cmake",
    "ctest",
    "make",
    "mvn test",
    "gradlew test",
    "npm test",
    "npx jest",
    "npx vitest",
    "eslint",
    "tsc",
    "dart analyze",
    "flutter test",
    "dotnet test",
    "dotnet format",
    "cpplint",
    "clang-tidy",
    "checkstyle",
}


def _is_command_allowed(command: str) -> bool:
    """检查命令是否在白名单内。"""
    stripped = command.strip()
    return any(stripped.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES)


class DockerSandbox(BaseSandboxEngine):
    """基于 docker-py 的沙盒执行引擎。

    特性：
    - asyncio 包装，不阻塞事件循环
    - 300 秒超时防护（Blueprint 第 5 节）
    - 自动拉取缺失镜像
    - Search/Replace Block 精确应用代码修改
    - 命令白名单安全拦截
    """

    def __init__(self) -> None:
        self._client: Optional[docker.DockerClient] = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def run(
        self,
        image: str,
        command: str,
        source_files: Dict[str, str],
        search_replace_blocks: Optional[List[Dict[str, Any]]] = None,
        timeout: int = 300,
    ) -> SandboxResult:
        """在 Docker 容器中隔离执行命令。

        流程：
        1. 校验命令白名单
        2. 应用 Search/Replace Block 到源文件
        3. 将源文件打包为 tar 归档
        4. 创建容器、注入文件、执行命令
        5. asyncio.wait_for 超时控制

        Args:
            image: Docker 镜像名，如 "golang:1.23-alpine"
            command: 测试/Lint 命令（必须在白名单内）
            source_files: 源文件映射 (file_path -> content)
            search_replace_blocks: 搜索/替换块列表
            timeout: 超时秒数，默认 300

        Returns:
            SandboxResult
        """
        # 安全校验：命令白名单
        if not _is_command_allowed(command):
            logger.error("命令被白名单拦截: %s", command)
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"Command blocked by whitelist: {command}",
                timed_out=False,
            )

        # 应用 Search/Replace Block
        if search_replace_blocks:
            source_files = self.apply_search_replace(source_files, search_replace_blocks)

        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, self._run_sync, image, command, source_files
                ),
                timeout=timeout,
            )
            return result
        except asyncio.TimeoutError:
            logger.error("沙盒执行超时 (%ds): image=%s, cmd=%s", timeout, image, command)
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"Sandbox execution timed out after {timeout}s",
                timed_out=True,
            )

    def _run_sync(
        self, image: str, command: str, source_files: Dict[str, str]
    ) -> SandboxResult:
        """同步执行沙盒命令（在线程池中运行）。"""
        client = self._get_client()
        container = None

        try:
            # 确保镜像存在
            try:
                client.images.get(image)
            except ImageNotFound:
                logger.info("镜像 %s 不存在，正在拉取...", image)
                client.images.pull(image)

            # 创建容器
            container = client.containers.run(
                image=image,
                command=["sh", "-c", command],
                detach=True,
                working_dir="/workspace",
                # 资源限制，防止滥用
                mem_limit="512m",
                cpu_period=100000,
                cpu_quota=50000,  # 0.5 CPU
                network_disabled=False,  # 需要网络下载依赖
            )

            # 将源文件注入容器
            tar_stream = self._create_tar(source_files)
            container.put_archive("/workspace", tar_stream)

            # 等待执行完成
            result = container.wait(timeout=310)
            exit_code = result.get("StatusCode", -1)

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            return SandboxResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=False,
            )

        except ContainerError as e:
            return SandboxResult(
                exit_code=e.exit_status,
                stdout="",
                stderr=str(e),
                timed_out=False,
            )
        except APIError as e:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=f"Docker API error: {e}",
                timed_out=False,
            )
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def _create_tar(self, source_files: Dict[str, str]) -> io.BytesIO:
        """将源文件打包为 tar 归档流。"""
        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            for file_path, content in source_files.items():
                source_bytes = content.encode("utf-8")
                info = tarfile.TarInfo(name=file_path)
                info.size = len(source_bytes)
                tar.addfile(info, io.BytesIO(source_bytes))
        tar_stream.seek(0)
        return tar_stream

    async def cleanup(self) -> None:
        """清理 Docker 客户端连接。"""
        if self._client:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._client.close)
            self._client = None
