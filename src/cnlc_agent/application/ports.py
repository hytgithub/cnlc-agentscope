"""基础设施适配器实现这些端口，核心业务不直接持有第三方 SDK 连接。"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.execution import (
    Execution,
    ExecutionStatus,
    ExecutionTrigger,
    InterpretationTask,
    PlanningReason,
)
from cnlc_agent.domain.inputs import InputSource, InterpretationInputVersion
from cnlc_agent.domain.models import Contract, JsonObject, MockFixture, WellData, WellId
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolRun, ToolRunStatus


class WellRepository(Protocol):
    """井资料读取端口。"""

    async def load(self, well_id: str) -> WellData: ...


class TaskRepository(Protocol):
    """持续任务、版本化执行与旧版当前快照读取的长期存储端口。"""

    async def create_task(self, state: InterpretationState) -> None: ...

    async def get_task(self, task_id: str) -> InterpretationTask | None: ...

    async def create_input_version(
        self, task_id: str, fixture: MockFixture, source_type: InputSource = "UPLOAD"
    ) -> InterpretationInputVersion: ...

    async def get_input_version(
        self, input_version_id: str
    ) -> InterpretationInputVersion | None: ...

    async def list_input_versions(self, task_id: str) -> list[InterpretationInputVersion]: ...

    async def create_execution(
        self,
        state: InterpretationState,
        trigger_type: ExecutionTrigger = "RERUN",
        sequence: int | None = None,
        input_version_id: str | None = None,
        override_snapshot: InterpretationOverride | None = None,
        start_step: StepId | None = StepId.W01,
        source_execution_id: str | None = None,
        planning_reason: PlanningReason = "INITIAL",
        expected_current_execution_id: str | None = None,
    ) -> Execution: ...

    async def get_execution(self, execution_id: str) -> Execution | None: ...

    async def list_executions(self, task_id: str) -> list[Execution]: ...

    async def claim_execution(
        self, execution_id: str, worker_id: str, lease_expires_at: datetime
    ) -> bool: ...

    async def renew_execution_lease(
        self, execution_id: str, worker_id: str, lease_expires_at: datetime
    ) -> bool: ...

    async def finish_execution(
        self,
        execution_id: str,
        worker_id: str,
        status: ExecutionStatus,
        error_code: str | None = None,
    ) -> Execution: ...

    async def recover_expired_executions(self, now: datetime) -> list[str]: ...

    async def create_tool_run(self, run: ToolRun) -> ToolRun: ...

    async def finish_tool_run(
        self,
        tool_run_id: str,
        *,
        status: ToolRunStatus,
        source: str,
        output_snapshot: JsonObject,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ToolRun: ...

    async def get_tool_run(self, tool_run_id: str) -> ToolRun | None: ...

    async def list_tool_runs(self, execution_id: str) -> list[ToolRun]: ...

    async def get_execution_report(self, execution_id: str) -> str | None: ...

    async def save_execution_state(self, state: InterpretationState) -> None: ...

    async def save_execution_report(self, execution_id: str, markdown: str) -> None: ...

    async def set_current_execution(self, task_id: str, execution_id: str) -> None: ...

    # 旧接口保持 CLI、CheckpointStore 和 run_well_interpretation 的当前版本语义。
    async def create(self, state: InterpretationState) -> None: ...

    async def get_report(self, task_id: str) -> str | None: ...

    async def save(self, state: InterpretationState, markdown: str) -> None: ...

    async def get(self, task_id: str) -> InterpretationState | None: ...


class InterpretationStateStore(Protocol):
    """Workflow 运行时状态快照端口。"""

    async def save(self, state: InterpretationState) -> None: ...

    async def get(self, task_id: str) -> InterpretationState | None: ...


class ModelRequest(Contract):
    """发送到统一模型网关的最小结构化请求。"""

    task_id: str
    trace_id: str
    well_id: WellId
    purpose: str
    context: JsonObject


class ModelGateway(Protocol):
    """屏蔽模型供应商和传输协议差异的访问端口。"""

    async def generate(self, request: ModelRequest) -> JsonObject: ...


class Telemetry(Protocol):
    """业务事件和耗时跨度的可观测性端口。"""

    def span(self, name: str, attributes: JsonObject) -> AbstractContextManager[None]: ...

    def event(self, name: str, attributes: JsonObject) -> None: ...


class ExecutionDispatcher(Protocol):
    """后台调度端口；专业执行逻辑由提交的应用服务回调承担。"""

    async def submit(
        self, execution_id: str, execute: Callable[[str], Awaitable[None]]
    ) -> None: ...

    async def shutdown(self) -> None: ...
