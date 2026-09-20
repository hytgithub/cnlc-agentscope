from cnlc_agent.application.ports import ModelGateway, ModelRequest, Telemetry
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import JsonObject, StageResult
from cnlc_agent.tools.contracts import Tool, ToolCaller, ToolInput


class InterpretationAgent:
    """W06/W07 facade. Receives a context, never a mutable InterpretationState."""

    def __init__(
        self,
        gateway: ModelGateway,
        sw_tool: Tool,
        caller: ToolCaller,
        telemetry: Telemetry,
    ) -> None:
        self.gateway = gateway
        self.sw_tool = sw_tool
        self.caller = caller
        self.telemetry = telemetry

    async def run(self, request: ModelRequest) -> StageResult:
        attributes: JsonObject = {
            "agent": "InterpretationAgent",
            "task_id": request.task_id,
            "trace_id": request.trace_id,
            "purpose": request.purpose,
        }
        with self.telemetry.span("agent", attributes):
            if request.purpose == "fluid":
                output = await self.caller.call(
                    self.sw_tool,
                    ToolInput(
                        task_id=request.task_id,
                        trace_id=request.trace_id,
                        well_id=request.well_id,
                        step_id=StepId.W06,
                        parameters=request.context,
                    ),
                )
                request = request.model_copy(deep=True)
                request.context["sw_result"] = output.data
            with self.telemetry.span("model", attributes):
                result = StageResult.model_validate(await self.gateway.generate(request))
            # Keep deterministic Tool evidence in the structured result, including in mock mode.
            if request.purpose == "fluid":
                result.result["sw_result"] = request.context["sw_result"]
                result.evidence.append("calculate_sw Tool output (mock fixture)")
            return result
