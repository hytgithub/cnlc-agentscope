"""显式的进程内 Demo 适配器，不可冒充 PostgreSQL 或 Redis 持久化。"""

import asyncio
import json
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import DataError, InfrastructureError, ModelError
from cnlc_agent.domain.execution import Execution, ExecutionTrigger, InterpretationTask
from cnlc_agent.domain.inputs import (
    InputSource,
    InterpretationInputVersion,
    fixture_digest,
)
from cnlc_agent.domain.models import JsonObject, MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState


class FixtureRepository(Protocol):
    """从演示资料源读取 MockFixture 的最小端口。"""

    async def load(self, well_id: str) -> MockFixture: ...


class MockWellRepository:
    """仅允许从配置目录读取按井号命名的 JSON Fixture。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def load(self, well_id: str) -> MockFixture:
        """校验井号和路径边界后，异步读取并验证演示井资料。"""

        try:
            TaskRequest(well_id=well_id)
        except SchemaError as exc:
            raise DataError("INVALID_WELL_ID", "井标识格式无效") from exc
        path = (self.root / f"{well_id}.json").resolve()
        if not path.is_relative_to(self.root):
            raise DataError("INVALID_WELL_PATH", "井数据文件必须位于配置的数据目录中")
        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            fixture = MockFixture.model_validate_json(content)
        except FileNotFoundError as exc:
            raise DataError("WELL_NOT_FOUND", f"找不到演示井：{well_id}") from exc
        except (SchemaError, json.JSONDecodeError, UnicodeError) as exc:
            raise DataError("INVALID_FIXTURE", "演示井数据不符合 0.1-skeleton Schema") from exc
        except OSError as exc:
            raise DataError("DATA_READ_FAILED", "无法读取演示井数据") from exc
        if fixture.well.well_id != well_id:
            raise DataError("WELL_ID_MISMATCH", "文件中的井标识与请求不一致")
        return fixture


class MockModelGateway:
    """回放 Fixture 预设响应，不调用 LLM、网络或专业计算。"""

    def __init__(self, repository: FixtureRepository) -> None:
        self.repository = repository

    async def generate(self, request: ModelRequest) -> JsonObject:
        """按 purpose 读取对应预设结果，保持与真实模型网关相同接口。"""

        fixture = await self.repository.load(request.well_id)
        if request.purpose == "validation":
            return fixture.validation.model_dump(mode="json")
        result = fixture.outputs.get(request.purpose)
        if result is None:
            raise ModelError("MOCK_RESPONSE_MISSING", f"缺少预设模型响应：{request.purpose}")
        return result.model_dump(mode="json")


class InMemoryStateStore:
    """仅供 Demo/测试；通过深拷贝防止调用方污染已保存快照。"""

    def __init__(self) -> None:
        self._states: dict[str, InterpretationState] = {}

    async def save(self, state: InterpretationState) -> None:
        self._states[state.task.task_id] = state.model_copy(deep=True)

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None


class InMemoryTaskRepository:
    """仅供 Demo/测试；与 PostgreSQL 使用相同的 Task/Execution 版本语义。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, InterpretationTask] = {}
        self._executions: dict[str, Execution] = {}
        self._task_executions: dict[str, list[str]] = {}
        self._inputs: dict[str, InterpretationInputVersion] = {}
        self._task_inputs: dict[str, list[str]] = {}
        self._states: dict[str, InterpretationState] = {}
        self.reports: dict[str, str] = {}

    async def create_task(self, state: InterpretationState) -> None:
        """保留旧当前快照读接口，同时建立持续任务。"""

        async with self._lock:
            task_id = state.task.task_id
            if task_id in self._tasks:
                raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务")
            self._tasks[task_id] = InterpretationTask(
                task_id=task_id,
                well_id=state.task.well_id,
                created_at=state.created_at,
                updated_at=state.updated_at,
            )
            self._states[task_id] = state.model_copy(deep=True)
            self.reports[task_id] = ""
            self._task_executions[task_id] = []
            self._task_inputs[task_id] = []

    async def get_task(self, task_id: str) -> InterpretationTask | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    async def create_input_version(
        self, task_id: str, fixture: MockFixture, source_type: InputSource = "UPLOAD"
    ) -> InterpretationInputVersion:
        """锁内分配序号；快照创建成功后才移动任务输入指针。"""

        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
            if task.well_id != fixture.well.well_id:
                raise InfrastructureError("TASK_WELL_MISMATCH", "输入井号与任务不一致")
            sequence = max(
                (self._inputs[item].sequence for item in self._task_inputs[task_id]), default=0
            ) + 1
            version = InterpretationInputVersion(
                task_id=task_id,
                well_id=task.well_id,
                sequence=sequence,
                source_type=source_type,
                content_sha256=fixture_digest(fixture),
                payload=fixture.model_copy(deep=True),
            )
            self._inputs[version.input_version_id] = version
            self._task_inputs[task_id].append(version.input_version_id)
            task.current_input_version_id = version.input_version_id
            task.updated_at = utc_now()
            return version.model_copy(deep=True)

    async def get_input_version(self, input_version_id: str) -> InterpretationInputVersion | None:
        """返回输入快照副本，调用方不能改写仓库中的历史版本。"""

        version = self._inputs.get(input_version_id)
        return version.model_copy(deep=True) if version is not None else None

    async def list_input_versions(self, task_id: str) -> list[InterpretationInputVersion]:
        """按创建顺序列出当前任务的输入版本。"""

        return [
            self._inputs[item].model_copy(deep=True) for item in self._task_inputs.get(task_id, [])
        ]

    async def create_execution(
        self,
        state: InterpretationState,
        trigger_type: ExecutionTrigger = "RERUN",
        sequence: int | None = None,
        input_version_id: str | None = None,
        override_snapshot: InterpretationOverride | None = None,
    ) -> Execution:
        """锁内分配序号并创建执行，随后才移动当前指针。"""

        async with self._lock:
            task_id = state.task.task_id
            task = self._tasks.get(task_id)
            if task is None:
                raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
            if task.well_id != state.task.well_id:
                raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
            if input_version_id is not None:
                input_version = self._inputs.get(input_version_id)
                if input_version is None:
                    raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
                if input_version.task_id != task_id:
                    raise InfrastructureError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于此任务")
            execution_id = state.workflow_execution_id
            if execution_id in self._executions:
                raise InfrastructureError("EXECUTION_EXISTS", "执行标识已存在")
            existing = [self._executions[item].sequence for item in self._task_executions[task_id]]
            next_sequence = max(existing, default=0) + 1 if sequence is None else sequence
            if next_sequence < 1 or next_sequence in existing:
                raise InfrastructureError("EXECUTION_SEQUENCE_EXISTS", "任务执行序号已存在或无效")
            now = utc_now()
            execution = Execution(
                execution_id=execution_id,
                task_id=task_id,
                sequence=next_sequence,
                status=state.status,
                state_snapshot=state.model_copy(deep=True),
                trigger_type=trigger_type,
                input_version_id=input_version_id,
                override_snapshot=(override_snapshot or InterpretationOverride()).model_copy(
                    deep=True
                ),
                created_at=now,
                updated_at=now,
            )
            self._executions[execution_id] = execution
            self._task_executions[task_id].append(execution_id)
            task.current_execution_id = execution_id
            task.updated_at = now
            self._states[task_id] = state.model_copy(deep=True)
            self.reports[task_id] = ""
            return execution.model_copy(deep=True)

    async def get_execution(self, execution_id: str) -> Execution | None:
        execution = self._executions.get(execution_id)
        return execution.model_copy(deep=True) if execution is not None else None

    async def list_executions(self, task_id: str) -> list[Execution]:
        return [
            self._executions[item].model_copy(deep=True)
            for item in self._task_executions.get(task_id, [])
        ]

    async def save_execution_state(self, state: InterpretationState) -> None:
        """仅当前执行可写；后续运行不会改写旧版本。"""

        async with self._lock:
            task_id = state.task.task_id
            execution_id = state.workflow_execution_id
            task = self._tasks.get(task_id)
            execution = self._executions.get(execution_id)
            if task is None or execution is None or execution.task_id != task_id:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
            if task.current_execution_id != execution_id:
                raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不可写入当前快照")
            if task.well_id != state.task.well_id:
                raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
            execution.state_snapshot = state.model_copy(deep=True)
            execution.status = state.status
            execution.updated_at = utc_now()
            self._states[task_id] = state.model_copy(deep=True)
            task.updated_at = execution.updated_at

    async def save_execution_report(self, execution_id: str, markdown: str) -> None:
        """报告写入当前 Execution，成功指针只在报告形成后移动。"""

        async with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
            task = self._tasks[execution.task_id]
            if task.current_execution_id != execution_id:
                raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不可改写报告")
            execution.markdown = markdown
            execution.updated_at = utc_now()
            self.reports[execution.task_id] = markdown
            task.updated_at = execution.updated_at
            if markdown and execution.status in {StepStatus.SUCCESS, StepStatus.WARNING}:
                task.latest_successful_execution_id = execution_id

    async def set_current_execution(self, task_id: str, execution_id: str) -> None:
        """仅允许切换到属于该任务的已存在版本。"""

        async with self._lock:
            task = self._tasks.get(task_id)
            execution = self._executions.get(execution_id)
            if task is None:
                raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
            if execution is None or execution.task_id != task_id:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
            current = self._executions.get(task.current_execution_id or "")
            if current is not None and execution.sequence < current.sequence:
                raise InfrastructureError("EXECUTION_NOT_LATEST", "不能将历史执行设为当前版本")
            task.current_execution_id = execution_id
            task.updated_at = utc_now()
            self._states[task_id] = execution.state_snapshot.model_copy(deep=True)
            self.reports[task_id] = execution.markdown

    async def create(self, state: InterpretationState) -> None:
        """首个任务与执行一起建立，避免部分创建。"""

        async with self._lock:
            task_id = state.task.task_id
            execution_id = state.workflow_execution_id
            if task_id in self._tasks:
                raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务")
            if execution_id in self._executions:
                raise InfrastructureError("EXECUTION_EXISTS", "执行标识已存在")
            now = utc_now()
            task = InterpretationTask(
                task_id=task_id,
                well_id=state.task.well_id,
                current_execution_id=execution_id,
                created_at=state.created_at,
                updated_at=now,
            )
            execution = Execution(
                execution_id=execution_id,
                task_id=task_id,
                sequence=1,
                status=state.status,
                state_snapshot=state.model_copy(deep=True),
                trigger_type="INITIAL",
                created_at=state.created_at,
                updated_at=now,
            )
            self._tasks[task_id] = task
            self._executions[execution_id] = execution
            self._task_executions[task_id] = [execution_id]
            self._task_inputs[task_id] = []
            self._states[task_id] = state.model_copy(deep=True)
            self.reports[task_id] = ""

    async def get_report(self, task_id: str) -> str | None:
        return self.reports.get(task_id)

    async def save(self, state: InterpretationState, markdown: str) -> None:
        """与数据库适配器一样原子更新当前执行及兼容视图。"""

        async with self._lock:
            task_id = state.task.task_id
            execution_id = state.workflow_execution_id
            task = self._tasks.get(task_id)
            execution = self._executions.get(execution_id)
            if task is None or execution is None or execution.task_id != task_id:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
            if task.current_execution_id != execution_id:
                raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不可写入当前快照")
            if task.well_id != state.task.well_id:
                raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
            now = utc_now()
            execution.state_snapshot = state.model_copy(deep=True)
            execution.status = state.status
            execution.markdown = markdown
            execution.updated_at = now
            self._states[task_id] = state.model_copy(deep=True)
            self.reports[task_id] = markdown
            task.updated_at = now
            if markdown and state.status in {StepStatus.SUCCESS, StepStatus.WARNING}:
                task.latest_successful_execution_id = execution_id

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None
