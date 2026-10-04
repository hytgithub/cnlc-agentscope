"""Task 012A 的独立 Mock Toolkit；所有返回值都显式标记为 Fixture。"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.tool import ParamsBase, ToolBase, ToolChunk
from pydantic import Field, ValidationError

from .state import MockState


class EmptyInput(ParamsBase):
    """无参数 Mock Tool 的空输入契约。"""


class StartInterpretationInput(ParamsBase):
    """完整解释 Mock Tool 的输入。"""

    well_id: str


class QueryResultInput(ParamsBase):
    """已有结果只读查询的输入。"""

    execution_id: str | None = None
    scope: str | None = None


class PreflightInput(ParamsBase):
    """修改预检输入；空目标或值会明确要求澄清。"""

    target: str | None = None
    value: float | None = None
    scope: str | None = Field(
        default=None,
        description="全井修改传 whole_well；层段范围必须显式提供，不能从只读查看上下文推断。",
    )


class ApplyChangeInput(ParamsBase):
    """修改应用入口输入；仍须匹配本状态中的成功预检。"""

    target: str
    value: float
    scope: str = "whole_well"


class CompareVersionsInput(ParamsBase):
    """结果版本对照输入，要求两个不同且已知的 Fixture 版本。"""

    left_execution_id: str
    right_execution_id: str


class ReadReportInput(ParamsBase):
    """读取已有报告的版本选择器。"""

    selector: Literal["CURRENT", "PREVIOUS", "LATEST_SUCCESSFUL"] = "CURRENT"


class ConfirmStageInput(ParamsBase):
    """确认当前 Fixture 中等待确认的阶段。"""

    stage: str | None = None


class NativePocTool(ToolBase):
    """以统一序列化边界验证参数、执行 Mock 并记录完整 Tool 合约。"""

    input_model: ClassVar[type[ParamsBase]] = EmptyInput
    is_concurrency_safe = False
    is_read_only = True

    def __init__(self, state: MockState) -> None:
        super().__init__()
        self.state = state
        self.input_schema = self.input_model.model_json_schema()

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """只允許本地 Fixture 实验调用，不代表生产系统权限策略。"""

        del tool_input, context
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Task 012A 本地 Mock Tool 不访问生产数据。",
        )

    async def call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        """校验 Tool 输入，执行单一职责处理并保存可复核调用记录。"""

        if args:
            result = {"status": "INVALID_INPUT", "message": "Tool 仅接受具名参数。"}
            arguments: dict[str, Any] = {"positional_argument_count": len(args)}
            chunk_state = ToolResultState.ERROR
        else:
            try:
                parsed = self.input_model.model_validate(kwargs)
            except ValidationError:
                result = {"status": "INVALID_INPUT", "message": "Tool 输入不符合 Mock Schema。"}
                arguments = dict(kwargs)
                chunk_state = ToolResultState.ERROR
            else:
                arguments = parsed.model_dump(mode="json", exclude_unset=True)
                result = self.execute(parsed)
                chunk_state = (
                    ToolResultState.ERROR
                    if result.get("status") in {"INVALID_INPUT", "REJECTED", "NOT_FOUND"}
                    else ToolResultState.SUCCESS
                )

        self.state.record(self.name, arguments, result)
        return ToolChunk(
            content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
            state=chunk_state,
            metadata={"result": result, "fixture": True},
        )

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        """由具体 Mock Tool 实现自身边界；不调用正式算法或业务服务。"""

        raise NotImplementedError


class GetCurrentContextTool(NativePocTool):
    """读取本实验的 task、版本和查看范围 Fixture。"""

    name = "get_current_context"
    description = "读取 Task 012A 测试 Fixture 的当前井、版本、查看层段和待确认阶段。"
    input_model = EmptyInput

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        del parsed
        return {"status": "OK", **self.state.context()}


class StartFullInterpretationTool(NativePocTool):
    """记录一次合成的完整解释请求，不运行 Workflow 或测井算法。"""

    name = "start_full_interpretation"
    description = "仅当用户明确要求整井完整解释时，启动 Task 012A 的模拟解释。"
    input_model = StartInterpretationInput
    is_read_only = False

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = StartInterpretationInput.model_validate(parsed)
        if request.well_id != self.state.well_id:
            return {"status": "NOT_FOUND", "fixture": True, "well_id": request.well_id}
        execution_id = self.state.new_execution()
        self.state.pending_stage = "report_generation"
        return {
            "status": "SIMULATED",
            "fixture": True,
            "message": "仅记录 Task 012A Fixture 请求；未执行测井计算。",
            "task_id": self.state.task_id,
            "well_id": self.state.well_id,
            "execution_id": execution_id,
            "stages": [
                "data_decode",
                "preprocessing",
                "intelligent_processing",
                "report_generation",
            ],
        }


class QueryInterpretationResultTool(NativePocTool):
    """读取合成 Fixture 结果，不启动新执行。"""

    name = "query_interpretation_result"
    description = "只读查询已有 Fixture 解释结果，可限定明确版本或查看层段，不创建执行。"
    input_model = QueryResultInput

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = QueryResultInput.model_validate(parsed)
        execution_id = request.execution_id or self.state.current_execution_id
        if execution_id not in self.state.known_execution_ids:
            return {"status": "NOT_FOUND", "fixture": True, "execution_id": execution_id}
        return {
            "status": "OK",
            "fixture": True,
            "source": "task_012a_test_fixture",
            "is_real_business_data": False,
            "task_id": self.state.task_id,
            "well_id": self.state.well_id,
            "execution_id": execution_id,
            "scope": request.scope or self.state.view_scope,
            "summary": "仅有 Task 012A Mock Fixture；不包含真实测井结果或专业计算值。",
        }


class PreflightModifyParameterTool(NativePocTool):
    """检查目标、范围和值是否属于有限的 Mock 修改能力。"""

    name = "preflight_modify_parameter"
    description = (
        "所有修改前必须调用。判断目标、范围和值是否明确且由 Task 012A Mock 支持；"
        "全井修改传 scope=whole_well，范围缺失时不得默认全井。"
        "返回 ALLOWED、UNSUPPORTED 或 NEED_CLARIFICATION。"
    )
    input_model = PreflightInput
    is_read_only = False

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = PreflightInput.model_validate(parsed)
        self.state.approved_change = None
        if not request.target or request.value is None:
            return {
                "status": "NEED_CLARIFICATION",
                "fixture": True,
                "message": "修改目标或数值缺失；请补充后重新预检。",
            }

        normalized = self._normalize_target(request.target)
        if normalized is None:
            return {
                "status": "UNSUPPORTED",
                "fixture": True,
                "message": "该参数在 Task 012A Mock 中未开放，未执行修改。",
            }

        if request.scope is None:
            return {
                "status": "NEED_CLARIFICATION",
                "fixture": True,
                "message": "修改范围未明确；请说明全井或具体层段，未执行修改。",
            }

        scope = request.scope.strip().lower()
        if scope not in {"whole_well", "well", "全井"}:
            return {
                "status": "UNSUPPORTED",
                "fixture": True,
                "message": "层段级参数修改在 Task 012A Mock 中未开放，未执行修改。",
            }

        approved = {"target": normalized, "value": request.value, "scope": "whole_well"}
        self.state.approved_change = approved
        return {
            "status": "ALLOWED",
            "fixture": True,
            "message": "Mock 仅允许模拟全井参数修改；这不代表正式系统能力。",
            **approved,
        }

    @staticmethod
    def _normalize_target(target: str) -> str | None:
        value = target.strip().lower().replace("_", "").replace("-", "")
        if value in {"por", "porosity", "孔隙度"}:
            return "POROSITY"
        if value in {"perm", "permeability", "渗透率"}:
            return "PERMEABILITY"
        return None


class ApplyParameterChangeTool(NativePocTool):
    """只在同一目标和值通过预检后，推进 Mock 版本号。"""

    name = "apply_parameter_change"
    description = "仅可在匹配的 preflight_modify_parameter 返回 ALLOWED 后模拟应用修改。"
    input_model = ApplyChangeInput
    is_read_only = False

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = ApplyChangeInput.model_validate(parsed)
        target = PreflightModifyParameterTool._normalize_target(request.target)
        approved = self.state.approved_change
        requested = {"target": target, "value": request.value, "scope": request.scope}
        if approved is None or approved != requested:
            return {
                "status": "REJECTED",
                "fixture": True,
                "message": "没有匹配的成功预检；Fixture 未发生变化。",
            }
        execution_id = self.state.new_execution()
        self.state.approved_change = None
        return {
            "status": "SIMULATED",
            "fixture": True,
            "is_real_business_data": False,
            "task_id": self.state.task_id,
            "well_id": self.state.well_id,
            "execution_id": execution_id,
            "target": target,
            "value": request.value,
            "message": "仅模拟版本更新；没有运行依赖计算或专业算法。",
        }


class CompareResultVersionsTool(NativePocTool):
    """比较两个已经存在的 Mock 版本，不派生专业结论。"""

    name = "compare_result_versions"
    description = "只比较两个不同且明确的已有 Fixture 版本，不创建新执行。"
    input_model = CompareVersionsInput

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = CompareVersionsInput.model_validate(parsed)
        left = request.left_execution_id.strip()
        right = request.right_execution_id.strip()
        if (
            left == right
            or left not in self.state.known_execution_ids
            or right not in self.state.known_execution_ids
        ):
            return {
                "status": "NEED_CLARIFICATION",
                "fixture": True,
                "message": "需要两个不同且已知的 Fixture 版本。",
            }
        return {
            "status": "OK",
            "fixture": True,
            "left_execution_id": left,
            "right_execution_id": right,
            "summary": "Mock 版本比较占位结果；不包含专业参数差异。",
        }


class ReadReportTool(NativePocTool):
    """读取已有的 Fixture 报告占位内容。"""

    name = "read_report"
    description = "读取已有 Fixture 报告；支持当前版、上一版或最近成功版选择。"
    input_model = ReadReportInput

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = ReadReportInput.model_validate(parsed)
        execution_id = {
            "CURRENT": self.state.current_execution_id,
            "PREVIOUS": self.state.previous_execution_id,
            "LATEST_SUCCESSFUL": self.state.current_execution_id,
        }[request.selector]
        return {
            "status": "OK",
            "fixture": True,
            "task_id": self.state.task_id,
            "well_id": self.state.well_id,
            "execution_id": execution_id,
            "selector": request.selector,
            "report": "Task 012A Fixture 报告占位文本；不是实际测井解释报告。",
        }


class ConfirmStageTool(NativePocTool):
    """模拟确认 Fixture 当前等待阶段，不继续真实 Workflow。"""

    name = "confirm_stage"
    description = "用户明确确认并继续时，确认当前 Fixture 中等待确认的阶段。"
    input_model = ConfirmStageInput
    is_read_only = False

    def execute(self, parsed: ParamsBase) -> dict[str, Any]:
        request = ConfirmStageInput.model_validate(parsed)
        pending = self.state.pending_stage
        if pending is None:
            return {"status": "REJECTED", "fixture": True, "message": "当前没有等待确认的阶段。"}
        if request.stage is not None and request.stage != pending:
            return {
                "status": "NEED_CLARIFICATION",
                "fixture": True,
                "pending_stage": pending,
                "message": "确认目标与 Fixture 当前等待阶段不一致。",
            }
        self.state.pending_stage = None
        return {
            "status": "SIMULATED",
            "fixture": True,
            "execution_id": self.state.current_execution_id,
            "confirmed_stage": pending,
            "message": "仅更新 Task 012A Mock 状态；没有继续真实 Workflow。",
        }


def build_mock_tools(state: MockState) -> list[NativePocTool]:
    """创建一组共享同一内存 Fixture 的八个独立业务 Mock Tool。"""

    return [
        GetCurrentContextTool(state),
        StartFullInterpretationTool(state),
        QueryInterpretationResultTool(state),
        PreflightModifyParameterTool(state),
        ApplyParameterChangeTool(state),
        CompareResultVersionsTool(state),
        ReadReportTool(state),
        ConfirmStageTool(state),
    ]
