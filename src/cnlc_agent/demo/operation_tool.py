"""唯一模型侧后续操作 Tool；严格输入、安全投影，不含自然语言解析规则。"""

import json
import logging
from typing import Any
from uuid import uuid4

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from pydantic import ValidationError, model_validator

from cnlc_agent.demo.operation_clarification import ClarificationPatchError
from cnlc_agent.demo.operation_interaction import (
    ClarificationReplyRequest,
    OperationInteractionController,
    OperationRequest,
    OperationToolResult,
)
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.demo.tools import RunWellInterpretationTool
from cnlc_agent.domain.errors import ApplicationError
from cnlc_agent.domain.models import Contract
from cnlc_agent.domain.session_binding import TaskSessionIdentity

OPERATION_TOOL_NAME = "interpret_interpretation_operation"
ALLOWED_AGENT_TASK_TOOLS = frozenset({"run_well_interpretation", OPERATION_TOOL_NAME})


class OperationToolInput(Contract):
    """模型不能提交稳定层段身份，包括嵌套 constraints 和 shared_context。"""

    request: OperationRequest

    @model_validator(mode="before")
    @classmethod
    def reject_interval_identity(cls, value: Any) -> Any:
        """稳定身份只能由服务器 Resolver 产生，不能通过任意嵌套载荷夹带。"""

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                if any(key in item for key in ("interval_id", "interval_ids", "resolved_ids")):
                    raise ValueError("model must use interval ordinal references")
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return value


def model_input_schema() -> dict[str, Any]:
    """导出同一 Partial Schema 的模型安全子集，隐藏服务端稳定层段字段。"""
    schema = OperationToolInput.model_json_schema()
    forbidden = {"#/$defs/IntervalScope", "#/$defs/MultiIntervalScope"}

    def trim(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("anyOf", "oneOf"):
                if key in node:
                    node[key] = [v for v in node[key] if v.get("$ref") not in forbidden]
            if "discriminator" in node:
                mapping = node["discriminator"].get("mapping", {})
                node["discriminator"]["mapping"] = {
                    k: v for k, v in mapping.items() if v not in forbidden
                }
            for value in node.values():
                trim(value)
        elif isinstance(node, list):
            for value in node:
                trim(value)

    for name in ("IntervalScope", "MultiIntervalScope"):
        schema["$defs"].pop(name, None)
    schema["$defs"]["FilterSetScope"]["properties"].pop("resolved_ids", None)
    trim(schema)
    return schema


class InterpretInterpretationOperationTool(ToolBase):
    """接收有限语义并交给 Controller；沿用现有命令与流式报告契约。"""

    name = OPERATION_TOOL_NAME
    description = (
        "已有任务的唯一操作入口。PLAN 一次提交全部意图；"
        "CLARIFICATION_REPLY 补齐待澄清槽；CANCEL 取消；"
        "SET_ACTIVE_CONTEXT 显式切井。层段使用层号，禁止编造 interval_id。"
    )
    input_schema = model_input_schema()
    is_concurrency_safe = False
    is_read_only = False

    def __init__(self, runner: TaskCommandRunner) -> None:
        super().__init__()
        self.runner = runner
        self.controller = OperationInteractionController(runner)

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        """用户主动请求的有限操作仍由服务端完整授权和校验。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW, message="处理用户请求的测井操作"
        )

    async def call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        """拒绝原始异常泄露，输出只含稳定结果；Pending owner 不进入模型上下文。"""
        request = None
        try:
            if args:
                raise ValueError("keyword arguments required")
            request = OperationToolInput.model_validate(kwargs).request
            result = await self.controller.handle(request)
        except ClarificationPatchError:
            if isinstance(request, ClarificationReplyRequest):
                self.runner.retain_operation_clarification()
            result = OperationToolResult(
                outcome="REJECTED",
                error_code="CLARIFICATION_SLOT_INVALID",
                message="没有可补齐的有效操作，或本次槽位不能修改；请重新说明完整请求。",
            )
        except (ValidationError, ValueError, TypeError):
            result = OperationToolResult(
                outcome="REJECTED",
                error_code="INVALID_OPERATION_PLAN",
                message="操作结构无效，请明确任务、目标、范围和修改值。",
            )
        except ApplicationError as exc:
            result = OperationToolResult(
                outcome="REJECTED",
                error_code=exc.code,
                message="操作未能完成，请检查任务引用、版本或当前状态。",
            )
        except Exception as exc:
            logging.getLogger(__name__).warning("Operation request failed: %s", type(exc).__name__)
            result = OperationToolResult(
                outcome="REJECTED",
                error_code="TASK_COMMAND_FAILED",
                message="操作暂时无法完成，请稍后重试。",
            )
        payload = result.model_dump(mode="json")
        metadata: dict[str, Any] = {"operation": payload}
        if result.error_code:
            metadata.update(error_code=result.error_code, message=result.message)
        # 兼容现有 Streamer/Panel 的唯一 TaskCommandResult，不复制执行展示链。
        metadata["result"] = (
            result.task_results[0].model_dump(mode="json")
            if len(result.task_results) == 1
            else payload
        )
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=ToolResultState.SUCCESS
            if result.outcome in {"SUCCESS", "READ_ONLY"}
            else ToolResultState.ERROR,
            metadata=metadata,
        )


def build_agent_task_tools(runner: TaskCommandRunner | None = None) -> list[ToolBase]:
    """正式 Toolkit 仅两个入口；独立内存联调分配隔离身份，HTTP 使用工厂身份。"""
    runner = runner or TaskCommandRunner()
    if runner.session_identity is None:
        if runner.persistence.persistence != "memory":
            raise ValueError("persistent agent tools require session identity")
        runner.session_identity = TaskSessionIdentity(
            user_id="local", agent_id="demo", session_id=uuid4().hex
        )
    return [RunWellInterpretationTool(runner), InterpretInterpretationOperationTool(runner)]
