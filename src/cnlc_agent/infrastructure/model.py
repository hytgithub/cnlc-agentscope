"""Compatibility exports for the Task 04 model adapter."""

from cnlc_agent.infrastructure.model_gateway import (
    OpenAICompatibleGateway,
    OpenAICompatibleModelGateway,
)

__all__ = ["OpenAICompatibleGateway", "OpenAICompatibleModelGateway"]
