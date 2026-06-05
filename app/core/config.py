"""Pydantic Settings 全局配置加载 - 严格遵循 Blueprint 规范。

使用 pydantic-settings 从 config/settings.yaml 加载配置，
支持环境变量覆盖。
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"


class LLMRetryConfig(BaseModel):
    """LLM 重试策略配置。"""

    max_attempts: int = 5
    min_wait_seconds: float = 2.0
    max_wait_seconds: float = 10.0
    multiplier: int = 1


class LLMRoleConfig(BaseModel):
    """单个 LLM 角色配置。"""

    base_url: str
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    temperature: float = 0.3
    max_tokens: int = 4096

    @property
    def api_key(self) -> str:
        """从环境变量读取 API Key。"""
        key = os.getenv(self.api_key_env, "")
        if not key:
            raise ValueError(f"环境变量 {self.api_key_env} 未设置")
        return key


class LLMConfig(BaseModel):
    """LLM 配置。"""

    roles: Dict[str, LLMRoleConfig]
    retry: LLMRetryConfig = LLMRetryConfig()


class SandboxMatrixItem(BaseModel):
    """沙盒语言矩阵配置项 - V2 包含文件后缀映射。"""

    suffixes: list[str] = Field(default_factory=list, description="文件特征后缀")
    image: str
    test_command: str
    lint_command: str


class SandboxConfig(BaseModel):
    """沙盒配置。"""

    default_engine: str = "docker"
    timeout: int = 300
    matrix: Dict[str, SandboxMatrixItem]


class GitLabConfig(BaseModel):
    """GitLab 配置。"""

    api_url: str = "https://gitlab.example.com/api/v4"
    token_env: str = "GITLAB_TOKEN"

    @property
    def token(self) -> str:
        """从环境变量读取 GitLab Token。"""
        token = os.getenv(self.token_env, "")
        if not token:
            raise ValueError(f"环境变量 {self.token_env} 未设置")
        return token


class GitHubConfig(BaseModel):
    """GitHub 配置。"""

    api_url: str = "https://api.github.com"
    token_env: str = "GITHUB_TOKEN"

    @property
    def token(self) -> str:
        """从环境变量读取 GitHub Token。"""
        token = os.getenv(self.token_env, "")
        if not token:
            raise ValueError(f"环境变量 {self.token_env} 未设置")
        return token


class QueueConfig(BaseModel):
    """消息队列配置 (Phase 1: Redis Streams)。"""

    redis_url: str = "redis://localhost:6379/0"
    stream_key: str = "mr_review_queue"
    consumer_group: str = "review_workers"
    consumer_name: str = "worker-1"
    max_len: int = 10000


class CheckpointerConfig(BaseModel):
    """LangGraph 持久化检查点配置 (Phase 1: Postgres)。"""

    enabled: bool = True
    postgres_url: str = "postgresql://postgres:postgres@localhost:5432/autoreviewer"


class CircuitBreakerConfig(BaseModel):
    """API 熔断器配置 (Phase 1: pybreaker)。"""

    enabled: bool = True
    fail_max: int = 5
    reset_timeout: int = 30
    exclude_exceptions: list[str] = Field(default_factory=lambda: ["ValueError", "KeyError"])


class AppSettings(BaseSettings):
    """应用全局配置。"""

    llm: LLMConfig
    sandbox: SandboxConfig
    gitlab: GitLabConfig
    github: GitHubConfig = GitHubConfig()
    max_retry_count: int = 3

    # Phase 1
    queue: QueueConfig = QueueConfig()
    checkpointer: CheckpointerConfig = CheckpointerConfig()
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()

    model_config = {"arbitrary_types_allowed": True}


def load_settings() -> AppSettings:
    """从 settings.yaml 加载配置。

    Returns:
        AppSettings: 应用配置实例
    """
    if not SETTINGS_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在: {SETTINGS_FILE}")

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    return AppSettings(**raw_config)


# 全局单例
settings: AppSettings = load_settings()
