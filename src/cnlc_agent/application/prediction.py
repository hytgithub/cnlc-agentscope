"""专业预测的最小边界；当前只返回 Mock 上下文，不计算或编造地质结果。"""

from typing import Protocol

from cnlc_agent.domain.models import JsonObject
from cnlc_agent.domain.override import ExecutionContext


class PredictionProvider(Protocol):
    """接收 Execution 参数并提供专业 Tool 可用的来源上下文，与 LLM Gateway 无关。"""

    async def describe(self, context: ExecutionContext) -> JsonObject:
        """描述当前预测配置；真实文件预测协议留待后续 Heavy API Task 确定。"""
        ...


class MockPredictionProvider:
    """只回显有效参数和来源，不按模型 ID 人为改变 Fixture 的专业数值。"""

    async def describe(self, context: ExecutionContext) -> JsonObject:
        """返回预测上下文来源；专业结果来源由读取 Fixture 的 Tool 声明。"""

        return {
            "execution_id": context.execution_id,
            "input_version_id": context.input_version_id,
            "effective_parameters": context.effective_override.model_dump(mode="json"),
            "prediction_model": context.effective_override.prediction_model,
            "prediction_source": "mock:prediction",
            "is_mock": True,
            "parameter_propagated": True,
            "professionally_recalculated": False,
        }
