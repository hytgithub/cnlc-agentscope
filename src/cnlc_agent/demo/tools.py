"""Demo 服务向 AgentScope 暴露的唯一高层业务 Tool。"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from pydantic import BaseModel, ConfigDict

from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.presentation import DemoStep, present_steps
from cnlc_agent.domain.enums import StepId, StepStatus
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import MockFixture, TaskRequest
from cnlc_agent.domain.state import InterpretationState

RUN_TOOL_NAME = "run_well_interpretation"

ServiceContextFactory = Callable[
    [],
    AbstractAsyncContextManager[InterpretationTaskService],
]


class DemoToolResult(BaseModel):
    """供 AgentScope 渲染的精简稳定 Tool Result。"""

    model_config = ConfigDict(extra="forbid")

    status: StepStatus
    task_id: str
    well_id: str
    completed_steps: list[StepId]
    step_statuses: dict[StepId, StepStatus]
    steps: list[DemoStep]
    summary: str
    report_markdown: str


def _default_service_context() -> AbstractAsyncContextManager[InterpretationTaskService]:
    """按当前环境配置创建一次有明确资源生命周期的业务服务。"""

    settings = AppSettings(mode="demo")
    connections = ConnectionSettings()
    if settings.model_provider != "mock" and connections.model_name != "qwen-plus":
        raise ValueError("AgentScope Demo 的 MODEL_NAME 必须为 qwen-plus")
    return application_runtime(
        settings,
        persistence=PersistenceSettings(),
        connections=connections,
    )


class InterpretationToolRunner:
    """复用现有应用服务，不在 AgentScope Tool 中复制 Workflow 逻辑。"""

    def __init__(self, service_context: ServiceContextFactory = _default_service_context) -> None:
        self._service_context = service_context

    async def run(
        self, well_id: str, instruction: str = "执行单井测井解释骨架演示"
    ) -> DemoToolResult:
        """运行单井解释，并把完整 State 投影为受控的 DemoToolResult。"""

        async with self._service_context() as service:
            state, report_markdown = await service.run(
                TaskRequest(well_id=well_id, instruction=instruction)
            )
        return _demo_result(state, report_markdown)

    async def run_uploaded(
        self,
        fixture: MockFixture,
        instruction: str,
        materialize: Callable[[InterpretationInputVersion], Awaitable[None]],
    ) -> DemoToolResult:
        """上传先落为 InputVersion，再执行现有 W01～W10。"""

        async with self._service_context() as service:
            state, report_markdown = await service.run_with_input(
                TaskRequest(well_id=fixture.well.well_id, instruction=instruction),
                fixture,
                materialize,
            )
        return _demo_result(state, report_markdown)


def _demo_result(state: InterpretationState, report_markdown: str) -> DemoToolResult:
    """旧入口和上传入口共用相同的可视化结果投影。"""

    return DemoToolResult(
        status=state.status,
        task_id=state.task.task_id,
        well_id=state.task.well_id,
        completed_steps=state.completed_steps,
        step_statuses=_step_statuses(state),
        steps=present_steps(state),
        summary=_summary(state),
        report_markdown=report_markdown,
    )


async def run_uploaded_well(fixture: MockFixture, instruction: str) -> DemoToolResult:
    """InputVersion 是事实来源；临时目录仅适配现有 W01 Fixture 读取。"""

    # 临时目录在调用结束后删除，上传文件名不参与路径拼接。
    with TemporaryDirectory(prefix="cnlc-upload-") as directory:
        root = Path(directory)

        async def materialize(version: InterpretationInputVersion) -> None:
            """只从仓库读回的规范化快照生成本次执行的临时 Fixture。"""

            await asyncio.to_thread(
                (root / f"{version.well_id}.json").write_text,
                version.payload.model_dump_json(),
                encoding="utf-8",
            )

        settings = AppSettings(mode="demo", mock_data_dir=root)
        connections = ConnectionSettings()
        if settings.model_provider != "mock" and connections.model_name != "qwen-plus":
            raise ValueError("AgentScope Demo 的 MODEL_NAME 必须为 qwen-plus")
        runner = InterpretationToolRunner(
            lambda: application_runtime(settings, PersistenceSettings(), connections)
        )
        return await runner.run_uploaded(fixture, instruction, materialize)


def _step_statuses(state: InterpretationState) -> dict[StepId, StepStatus]:
    statuses = {step_id: StepStatus.PENDING for step_id in StepId}
    statuses.update({execution.step_id: execution.status for execution in state.executions})
    return statuses


def _summary(state: InterpretationState) -> str:
    if state.final_check is not None:
        summary = state.final_check.result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary
    completed = ", ".join(step.value for step in state.completed_steps) or "无"
    return f"井 {state.task.well_id} 解释任务状态为 {state.status.value}；已完成步骤：{completed}。"


_default_runner = InterpretationToolRunner()


async def run_well_interpretation(well_id: str) -> dict[str, object]:
    """对指定井执行现有 W01-W10 解释流程并返回摘要和 Markdown 报告。

    Args:
        well_id: 井标识，例如 ``WELL_MOCK_001``。
    """

    result = await _default_runner.run(well_id)
    return result.model_dump(mode="json")


def build_interpretation_tool(
    runner: InterpretationToolRunner | None = None,
) -> ToolBase:
    """构造 AgentScope 服务注册的唯一业务 Tool。"""

    return RunWellInterpretationTool(runner)


class RunWellInterpretationTool(ToolBase):
    """``run_well_interpretation`` 的 AgentScope Tool 包装器。"""

    name = RUN_TOOL_NAME
    description = (
        "对 well_id 执行现有单井常规测井解释流程 W01-W10，返回各步骤状态、"
        "解释摘要和 Markdown 报告。"
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "well_id": {
                "type": "string",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                "description": "待解释的井标识，例如 WELL_MOCK_001。",
            },
        },
        "required": ["well_id"],
        "additionalProperties": False,
    }
    is_concurrency_safe = True
    is_read_only = False

    def __init__(self, runner: InterpretationToolRunner | None = None) -> None:
        super().__init__()
        self._runner = runner
        self.upload: tuple[MockFixture, str] | None = None

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        """用户主动提交解释请求后，允许执行本地单井解释流程。"""

        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="用户请求执行单井解释时允许调用现有 InterpretationTaskService。",
        )

    async def call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        """把内部异常收敛为稳定 Tool 错误，避免向浏览器暴露堆栈和敏感配置。"""

        try:
            return await self._call(*args, **kwargs)
        except Exception as exc:
            logging.getLogger(__name__).warning("Demo tool failed: %s", type(exc).__name__)
            return ToolChunk(
                content=[TextBlock(text="解释任务失败，请检查井资料或服务配置后重试。")],
                state=ToolResultState.ERROR,
                metadata={"error_code": "DEMO_INTERPRETATION_FAILED"},
            )

    async def _call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        """选择上传 Fixture、默认 Runner 或测试注入 Runner，并统一结果外壳。"""

        if args:
            raise TypeError("run_well_interpretation 只接受关键字参数")
        well_id = cast(str, kwargs["well_id"])
        if self.upload is not None:
            # upload 只在当前回复期间设置，井号必须与 Tool 参数一致。
            fixture, instruction = self.upload
            if fixture.well.well_id != well_id:
                raise ValueError("上传井与任务井标识不一致")
            result = await run_uploaded_well(fixture, instruction)
            payload = result.model_dump(mode="json")
        elif self._runner is None:
            payload = await run_well_interpretation(well_id)
        else:
            result = await self._runner.run(well_id)
            payload = result.model_dump(mode="json")
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=(
                ToolResultState.SUCCESS
                if payload["status"] in {"SUCCESS", "WARNING"}
                else ToolResultState.ERROR
            ),
            metadata={"result": payload},
        )
