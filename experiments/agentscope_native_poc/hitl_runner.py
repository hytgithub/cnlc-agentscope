"""012D 隔离 HITL 适配：原生交互暂停包装已有阶段事实，不接入生产 UI。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agentscope.agent import Agent, ReActConfig
from agentscope.event import AgentEvent, UserConfirmResultEvent, UserInterruptEvent
from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import ParamsBase, ToolBase, ToolChunk, Toolkit
from pydantic import ConfigDict

from cnlc_agent.application.commands import GetReportCommand
from cnlc_agent.demo.operation_interaction import (
    ConfirmStageRequest,
    OperationInteractionController,
)
from cnlc_agent.demo.task_context import TaskReference
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.stages import InterpretationStage

REPORT_CONFIRMATION = "BUSINESS_CONFIRMATION_REQUIRED"


class CheckpointTarget(ParamsBase):
    """固定业务目标，不从用户确认事件的可编辑文本重新选择对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str
    execution_id: str
    stage: InterpretationStage
    stage_run_id: str

    @classmethod
    async def read(
        cls, runner: TaskCommandRunner, task_id: str, execution_id: str
    ) -> CheckpointTarget:
        """仅从现有进度投影构造待确认目标；调用后仍须写前重新校验。"""

        if not await runner.owns_task(task_id):
            raise InfrastructureError("TASK_NOT_FOUND", "任务不属于当前会话")
        progress = await runner.get_stage_progress(task_id, execution_id)
        if (
            progress.execution_status != "WAITING_CONFIRMATION"
            or progress.waiting_confirmation_stage is None
            or progress.current_stage_run_id is None
        ):
            raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "没有等待确认的阶段")
        return cls(
            task_id=task_id,
            execution_id=execution_id,
            stage=progress.waiting_confirmation_stage,
            stage_run_id=progress.current_stage_run_id,
        )


class ConfirmCheckpointTool(ToolBase):
    """确认一个已绑定的测试 checkpoint；权限暂停不能代替业务确认事务。"""

    name = "confirm_checkpoint"
    description = "确认已展示的 Mock 阶段结果并继续；拒绝时保留阶段候选，不取消外部 API。"
    is_read_only = False
    is_concurrency_safe = False

    def __init__(self, adapter: HitlAdapter, *, external: bool = False) -> None:
        super().__init__()
        self.adapter = adapter
        self.input_schema = CheckpointTarget.model_json_schema()
        # 外部模式只用于 H08 原生等待语义，不访问任何真实第三方 API。
        self.is_external_tool = external
        if external:
            self.name = "external_checkpoint"

    async def check_permissions(
        self, tool_input: dict[str, Any], context: PermissionContext
    ) -> PermissionDecision:
        """先核查固定目标与权威事实，再由原生 Agent 生成确认事件。"""

        del context
        self.adapter.check_arguments(tool_input)
        await self.adapter.validate_authority()
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW if self.is_external_tool else PermissionBehavior.ASK,
            message="012D 固定测试确认点，不定义产品强制确认规则。",
            bypass_immune=True,
        )

    async def call(self, **kwargs: Any) -> ToolChunk:
        """恢复后重新读取业务事实；只有现有确认 API 能推进同一 Execution。"""

        self.adapter.check_arguments(kwargs)
        await self.adapter.validate_authority()
        target = self.adapter.target
        execution = await self.adapter.runner.confirm_stage(
            target.task_id,
            target.execution_id,
            target.stage,
            target.stage_run_id,
            actor="012d-poc",
        )
        result = {
            "fixture": True,
            **target.model_dump(mode="json"),
            "execution_status": execution.status.value,
        }
        return ToolChunk(
            content=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
            metadata={"result": result},
        )


class HitlAdapter:
    """只绑定事件与业务目标；不保存第二套 pending 或 WAITING_CONFIRM 状态。"""

    def __init__(self, runner: TaskCommandRunner, target: CheckpointTarget) -> None:
        self.runner = runner
        self.target = target
        # 串行消费同一 Agent 的交互；业务跨请求/并发安全仍由 Repository 保证。
        self._lock = asyncio.Lock()

    def check_arguments(self, arguments: dict[str, Any]) -> None:
        """原生确认可改写参数，本 POC 明确禁止这种能力改变固定目标。"""

        if CheckpointTarget.model_validate(arguments) != self.target:
            raise ValueError("确认参数与已绑定目标不一致")

    async def validate_authority(self) -> None:
        """重查 ownership、Active、当前 Execution 与 StageRun；历史 event 无授权效力。"""

        target = self.target
        if not await self.runner.owns_task(target.task_id):
            raise InfrastructureError("TASK_NOT_FOUND", "任务不属于当前会话")
        if self.runner.active_task_id != target.task_id:
            raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "当前任务已切换")
        task = await self.runner.repository.get_task(target.task_id)
        if task is None or task.current_execution_id != target.execution_id:
            raise InfrastructureError("EXECUTION_NOT_CURRENT", "当前执行已改变")
        current = await CheckpointTarget.read(self.runner, target.task_id, target.execution_id)
        if current != target:
            raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "等待阶段已改变")

    def build_agent(
        self, model: ChatModelBase, *, state: AgentState | None = None, external: bool = False
    ) -> Agent:
        """模型由调用方注入；本实验不调用公网模型，不增加核心业务 Agent。"""

        return Agent(
            name="Task012DHitlPoc",
            system_prompt="通过提供的 Tool 展示测试 checkpoint；所有业务状态以 Tool 事实为准。",
            model=model,
            toolkit=Toolkit(tools=[ConfirmCheckpointTool(self, external=external)]),
            state=state,
            react_config=ReActConfig(max_iters=4),
        )

    async def current_confirm(self) -> None:
        """A 组复用当前 Operation / CONFIRM_STAGE 入口；目标预检不代表生产修复。"""

        async with self._lock:
            await self.validate_authority()
            await OperationInteractionController(self.runner).handle(
                ConfirmStageRequest(
                    mode="CONFIRM_STAGE",
                    task_reference=TaskReference(kind="TASK_ID", value=self.target.task_id),
                )
            )

    async def decline(self) -> None:
        """当前入口不定义业务拒绝终态；不确认时保留现有候选与运行事实。"""

        await self.validate_authority()

    @staticmethod
    def _check_reply(agent: Agent, reply_id: str) -> list[ToolCallBlock]:
        """框架不核对 reply_id，适配层必须绑定当前已暂停的 reply。"""

        calls = agent.state.get_awaiting_tool_calls(agent.name)
        if not calls or reply_id != agent.state.reply_id:
            raise ValueError("确认事件不属于当前等待的 reply")
        return calls

    async def resume(self, agent: Agent, event: UserConfirmResultEvent) -> list[AgentEvent]:
        """禁止改名、改参、空确认或注入持久允许规则，再交给原生 Agent 恢复。"""

        async with self._lock:
            calls = self._check_reply(agent, event.reply_id)
            if len(calls) != 1 or len(event.confirm_results) != 1:
                raise ValueError("必须明确提交唯一确认结果")
            result = event.confirm_results[0]
            original = calls[0]
            if (
                original.state != "asking"
                or original.name != "confirm_checkpoint"
                or result.tool_call.id != original.id
                or result.tool_call.name != original.name
                or result.rules
            ):
                raise ValueError("非法确认调用或权限规则")
            self.check_arguments(json.loads(original.input))
            self.check_arguments(json.loads(result.tool_call.input))
            await self.validate_authority()
            return [e async for e in agent.reply_stream(event)]

    async def interrupt(self, agent: Agent, event: UserInterruptEvent) -> list[AgentEvent]:
        """只停止已暂停的 Agent；不发送第三方取消命令、不改变 StageRun。"""

        async with self._lock:
            self._check_reply(agent, event.reply_id)
            return [e async for e in agent.reply_stream(event)]


async def read_existing_report(runner: TaskCommandRunner, task_id: str, execution_id: str) -> str:
    """读取已有报告走现有只读命令；未来生成确认策略保持待业务确认。"""

    if not await runner.owns_task(task_id):
        raise InfrastructureError("TASK_NOT_FOUND", "任务不属于当前会话")
    result = await runner.execute(GetReportCommand(task_id=task_id, execution_id=execution_id))
    return result.report_markdown or ""
