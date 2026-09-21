"""The composition root is the only place selecting mock implementations."""

from collections.abc import Awaitable, Callable

from cnlc_agent.agents.interpretation_agent import InterpretationAgent
from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.agents.validation_agent import ValidationAgent
from cnlc_agent.application.ports import InterpretationStateStore, ModelGateway, TaskRepository
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
    repository = MockWellRepository(settings.mock_data_dir)
    telemetry = LoggingTelemetry()
    caller = ToolCaller(telemetry, settings.tool_timeout_seconds)
    tools: dict[str, Tool] = {"get_well_data": GetWellDataTool(repository)}
    for name, key in {
        "check_curve_quality": "qc",
        "identify_lithology": "lithology",
        "evaluate_petrophysics": "petrophysics",
        "calculate_sw": "sw",
        "merge_intervals": "intervals",
    }.items():
        tools[name] = MockResultTool(name, key, repository)
    close_callbacks: list[Callable[[], Awaitable[None]]] = []
    gateway: ModelGateway
    if settings.model_provider == "mock":
        gateway = MockModelGateway(repository)
    elif settings.model_provider in {"openai_compatible", "openai-compatible", "real"}:
        gateway = OpenAICompatibleModelGateway.from_settings(settings, ConnectionSettings())
        close_callbacks.append(gateway.aclose)
    else:  # defensive guard for programmatic settings subclasses
        raise ValueError(f"unsupported model provider: {settings.model_provider}")
    interpretation = InterpretationAgent(gateway, tools["calculate_sw"], caller, telemetry)
    validation = ValidationAgent(gateway, telemetry)
    workflow = InterpretationWorkflow(
        build_steps(tools, caller, interpretation, validation),
        state_store if state_store is not None else InMemoryStateStore(),
        telemetry,
    )
    return InterpretationTaskService(
        MainAgent(workflow, telemetry),
        task_repository if task_repository is not None else InMemoryTaskRepository(),
        ReportAssembler(),
        telemetry,
        close_callbacks=close_callbacks,
    )
