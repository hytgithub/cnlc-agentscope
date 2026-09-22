from cnlc_agent.application.ports import ModelGateway, ModelRequest, Telemetry
from cnlc_agent.domain.models import ValidationResult


class ValidationAgent:
    """生成独立验证证据，不直接覆盖 InterpretationAgent 的解释结果。"""

    def __init__(self, gateway: ModelGateway, telemetry: Telemetry) -> None:
        self.gateway = gateway
        self.telemetry = telemetry

    async def run(self, request: ModelRequest) -> ValidationResult:
        """调用统一模型网关，并把响应校验为结构化验证结果。"""

        attributes = {
            "agent": "ValidationAgent",
            "task_id": request.task_id,
            "trace_id": request.trace_id,
        }
        with self.telemetry.span("agent", {**attributes}):
            with self.telemetry.span("model", {**attributes}):
                return ValidationResult.model_validate(await self.gateway.generate(request))
