from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNLC_", env_file=".env", extra="ignore")

    mode: Literal["mock"] = "mock"
    mock_data_dir: Path = Path("mock_data")
    output_dir: Path = Path("outputs")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    tool_timeout_seconds: float = Field(default=10, gt=0)


class ConnectionSettings(BaseSettings):
    """Connection secrets; persistence uses DB/Redis, other adapters remain deferred."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_name: str | None = None
    otel_exporter_otlp_endpoint: str | None = None


class PersistenceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CNLC_", env_file=".env", extra="ignore")

    persistence: Literal["memory", "postgres-redis"] = "memory"
    redis_prefix: str = Field(default="cnlc:v1", min_length=1)
    redis_ttl_seconds: int = Field(default=86400, gt=0)
    infrastructure_timeout_seconds: float = Field(default=5, gt=0)
