"""The composition root is the only place selecting mock implementations."""

from cnlc_agent.agents.interpretation_agent import InterpretationAgent
from cnlc_agent.agents.main_agent import MainAgent
from cnlc_agent.agents.validation_agent import ValidationAgent
from cnlc_agent.application.ports import InterpretationStateStore, TaskRepository
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings
from cnlc_agent.infrastructure.mock import (
    InMemoryStateStore,
    InMemoryTaskRepository,
    MockModelGateway,
    MockWellRepository,
)
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
    gateway = MockModelGateway(repository)
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
    )
