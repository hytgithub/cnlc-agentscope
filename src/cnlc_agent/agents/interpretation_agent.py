from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import ModelGateway, ModelRequest, Telemetry
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import JsonObject, StageResult
from cnlc_agent.domain.override import ExecutionContext
from cnlc_agent.tools.contracts import Tool, ToolCaller, ToolInput


class InterpretationAgent:
    """负责 W06/W07 的解释决策，只接收上下文快照，不直接修改全局状态。"""

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
        """调用确定性工具和模型，返回可由 Workflow 合并的结构化阶段结果。"""

        attributes: JsonObject = {
            "agent": "InterpretationAgent",
            "task_id": request.task_id,
            "trace_id": request.trace_id,
            "purpose": request.purpose,
        }
        with self.telemetry.span("agent", attributes):
            if request.purpose == "fluid":
                # 含水饱和度属于确定性计算，必须先调用 Tool；模型只综合 Tool 结果。
                output = await self.caller.call(
                    self.sw_tool,
                    ToolInput(
                        task_id=request.task_id,
                        trace_id=request.trace_id,
                        well_id=request.well_id,
                        step_id=StepId.W06,
                        # Sw Tool 只收受控执行参数，不透传完整模型上下文或曲线。
                        parameters=ExecutionContext.model_validate(
                            {
                                key: request.context[key]
                                for key in ExecutionContext.model_fields
                                if key in request.context
                            }
                        ).model_dump(mode="json"),
                    ),
                )
                request = request.model_copy(deep=True)
                request.context["sw_result"] = output.data
            with self.telemetry.span("model", attributes):
                raw_result = await self.gateway.generate(request)
                try:
                    result = StageResult.model_validate(raw_result)
                except SchemaError:
                    # Demo 允许兼容仅返回业务 JSON 的模型，正式模式必须严格满足统一外壳。
                    if request.context.get("execution_mode") != "demo":
                        raise
                    result = StageResult(
                        result=raw_result,
                        evidence=["Demo Mode：模型返回 JSON object，已补齐阶段结果外壳"],
                        is_mock=False,
                        source="model:demo-normalized",
                    )
            # 即使处于 Mock 模式，也要把确定性 Tool 证据写入结构化结果，便于审计来源。
            if request.purpose == "fluid":
                result.result["sw_result"] = request.context["sw_result"]
                result.evidence.append("calculate_sw Tool output (mock fixture)")
            return result
