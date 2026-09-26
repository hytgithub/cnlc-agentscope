"""应用组合根：集中选择真实或 Mock 实现，避免业务层自行判断基础设施。"""

from collections.abc import Awaitable, Callable

from cnlc_agent.agents.interpretation_agent import InterpretationAgent
from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.agents.validation_agent import ValidationAgent
from cnlc_agent.application.checkpoints import CheckpointStore
from cnlc_agent.application.ports import InterpretationStateStore, ModelGateway, TaskRepository
from cnlc_agent.application.prediction import MockPredictionProvider
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings, ConnectionSettings
from cnlc_agent.infrastructure.mock import (
    InMemoryStateStore,
    InMemoryTaskRepository,
    MockModelGateway,
    MockWellRepository,
)
from cnlc_agent.infrastructure.model_gateway import OpenAICompatibleModelGateway
from cnlc_agent.infrastructure.telemetry import LoggingTelemetry
from cnlc_agent.reports.assembler import ReportAssembler
from cnlc_agent.tools.contracts import Tool, ToolCaller
from cnlc_agent.tools.mock import GetWellDataTool, MockResultTool
from cnlc_agent.workflows.interpretation_workflow import InterpretationWorkflow
from cnlc_agent.workflows.steps import build_steps


def build_application(
    settings: AppSettings,
    *,
    task_repository: TaskRepository | None = None,
    state_store: InterpretationStateStore | None = None,
) -> InterpretationTaskService:
    """按配置装配任务服务及其 Agent、Workflow、Tool 和基础设施依赖。"""

    # 所有 Mock Tool 仍遵循正式 Tool Contract，未来可按名称替换为真实实现。
    repository = MockWellRepository(settings.mock_data_dir)
    telemetry = LoggingTelemetry()
    active_tasks = task_repository if task_repository is not None else InMemoryTaskRepository()
    caller = ToolCaller(telemetry, settings.tool_timeout_seconds, active_tasks)
    tools: dict[str, Tool] = {"get_well_data": GetWellDataTool(repository)}
    prediction = MockPredictionProvider()
    for name, key in {
        "check_curve_quality": "qc",
        "identify_lithology": "lithology",
        "evaluate_petrophysics": "petrophysics",
        "calculate_sw": "sw",
        "merge_intervals": "intervals",
    }.items():
        tools[name] = MockResultTool(name, key, repository, prediction)
    close_callbacks: list[Callable[[], Awaitable[None]]] = []
    gateway: ModelGateway
    if settings.model_provider == "mock":
        gateway = MockModelGateway(repository)
    elif settings.model_provider in {"openai_compatible", "openai-compatible", "real"}:
        gateway = OpenAICompatibleModelGateway.from_settings(settings, ConnectionSettings())
        close_callbacks.append(gateway.aclose)
    else:  # 防御式保护：防止程序化构造配置时绕过 Literal 校验。
        raise ValueError(f"unsupported model provider: {settings.model_provider}")
    interpretation = InterpretationAgent(gateway, tools["calculate_sw"], caller, telemetry)
    validation = ValidationAgent(gateway, telemetry)
    # Workflow 只依赖端口；未注入持久化实现时显式使用进程内 Demo 存储。
    workflow = InterpretationWorkflow(
        build_steps(
            tools,
            caller,
            interpretation,
            validation,
            demo_mode=settings.mode == "demo",
        ),
        (
            state_store
            if isinstance(state_store, CheckpointStore)
            else CheckpointStore(
                active_tasks,
                state_store if state_store is not None else InMemoryStateStore(),
            )
        ),
        telemetry,
    )
    return InterpretationTaskService(
        MainAgent(workflow, telemetry),
        active_tasks,
        ReportAssembler(settings.report_style),
        telemetry,
        mode=settings.mode,
        close_callbacks=close_callbacks,
    )
