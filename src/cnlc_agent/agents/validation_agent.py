from cnlc_agent.application.ports import ModelGateway, ModelRequest, Telemetry
from cnlc_agent.domain.models import ValidationResult


class ValidationAgent:
    """Returns independent evidence; cannot overwrite the interpretation state."""

    def __init__(self, gateway: ModelGateway, telemetry: Telemetry) -> None:
        self.gateway = gateway
        self.telemetry = telemetry

    async def run(self, request: ModelRequest) -> ValidationResult:
        attributes = {
            "agent": "ValidationAgent",
            "task_id": request.task_id,
            "trace_id": request.trace_id,
        }
        with self.telemetry.span("agent", {**attributes}):
            with self.telemetry.span("model", {**attributes}):
                return ValidationResult.model_validate(await self.gateway.generate(request))
