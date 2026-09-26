from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from cnlc_agent.reports.models import ReportStyle


class AppSettings(BaseSettings):
    """应用行为配置，统一读取带 ``CNLC_`` 前缀的环境变量。"""

    model_config = SettingsConfigDict(env_prefix="CNLC_", env_file=".env", extra="ignore")

    mode: Literal["mock", "demo"] = "mock"
    # company_mock 通过四个大步骤返回既有演示数据，不访问公司内网。
    professional_provider: Literal["fixture", "company_mock"] = "fixture"
    model_provider: Literal["mock", "openai_compatible", "openai-compatible", "real"] = "mock"
    mock_data_dir: Path = Path("mock_data")
    output_dir: Path = Path("outputs")
    report_style: ReportStyle = ReportStyle.STANDARD
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    tool_timeout_seconds: float = Field(default=10, gt=0)
    model_timeout_seconds: float = Field(default=30, gt=0)
    model_max_retries: int = Field(default=2, ge=0, le=5)
    model_retry_backoff_seconds: float = Field(default=0.25, ge=0, le=5)
    stream_step_delay_seconds: float = Field(default=1.0, ge=0, le=5)
    stream_report_chunk_delay_seconds: float = Field(default=0.12, ge=0, le=2)


class ConnectionSettings(BaseSettings):
    """数据库、Redis 和模型服务连接配置；敏感值使用 SecretStr 包装。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    model_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MODEL_BASE_URL", "CNLC_MODEL_BASE_URL"),
    )
    model_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MODEL_API_KEY", "DASHSCOPE_API_KEY", "CNLC_MODEL_API_KEY"),
    )
    model_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MODEL_NAME", "CNLC_MODEL_NAME"),
    )
    otel_exporter_otlp_endpoint: str | None = None


class PersistenceSettings(BaseSettings):
    """任务持久化模式、Redis 命名空间和基础设施超时配置。"""

    model_config = SettingsConfigDict(env_prefix="CNLC_", env_file=".env", extra="ignore")

    persistence: Literal["memory", "postgres-redis"] = "memory"
    redis_prefix: str = Field(default="cnlc:v1", min_length=1)
    redis_ttl_seconds: int = Field(default=86400, gt=0)
    infrastructure_timeout_seconds: float = Field(default=5, gt=0)
    execution_lease_seconds: float = Field(default=90, gt=3, le=3600)
    execution_poll_seconds: float = Field(default=15, gt=0, le=60)
