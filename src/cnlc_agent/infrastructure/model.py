"""保留 Task 04 模型适配器旧导入路径的兼容导出。"""

from cnlc_agent.infrastructure.model_gateway import (
    OpenAICompatibleGateway,
    OpenAICompatibleModelGateway,
)

__all__ = ["OpenAICompatibleGateway", "OpenAICompatibleModelGateway"]
