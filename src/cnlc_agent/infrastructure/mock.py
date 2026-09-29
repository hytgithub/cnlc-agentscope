"""显式的进程内 Demo 适配器，不可冒充 PostgreSQL 或 Redis 持久化。"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.domain.company_provider import (
    CompanyProviderCall,
    CompanyProviderCallStatus,
    CompanyProviderOperation,
)
from cnlc_agent.domain.dataset_revision import (
    DatasetChangeSet,
    DatasetRevision,
    change_set_digest,
    child_lineage,
    root_lineage,
)
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import DataError, InfrastructureError, ModelError
from cnlc_agent.domain.execution import (
    TERMINAL_EXECUTION_STATUSES,
    Execution,
    ExecutionRunMode,
    ExecutionStatus,
    ExecutionTrigger,
    InterpretationTask,
    PlanningReason,
    execution_status_from_state,
)
from cnlc_agent.domain.inputs import (
    InputSource,
    InterpretationInputVersion,
    fixture_digest,
)
from cnlc_agent.domain.models import JsonObject, MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.domain.stages import (
    InterpretationStage,
    StageRunStatus,
    StageValidity,
    confirm_stage,
)
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolRun, ToolRunStatus


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
        self._tool_runs: dict[str, ToolRun] = {}
        self._execution_tool_runs: dict[str, list[str]] = {}
        self._company_provider_calls: dict[str, CompanyProviderCall] = {}
        self._task_inputs: dict[str, list[str]] = {}
        self._dataset_revisions: dict[str, DatasetRevision] = {}
        self._dataset_change_sets: dict[str, DatasetChangeSet] = {}
        self._task_dataset_revisions: dict[str, list[str]] = {}
        self._states: dict[str, InterpretationState] = {}
        self.reports: dict[str, str] = {}
        self._session_task_bindings: dict[
            tuple[str, str, str, str], SessionTaskBinding
        ] = {}

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
            self._task_dataset_revisions[task_id] = []

    async def get_task(self, task_id: str) -> InterpretationTask | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    async def bind_task_to_session(self, binding: SessionTaskBinding) -> None:
        """同一绑定幂等写入；任务不存在时不能形成悬空授权。"""

        binding = SessionTaskBinding.model_validate(binding.model_dump(mode="python"))
        async with self._lock:
            if binding.task_id not in self._tasks:
                raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
            key = (
                binding.user_id,
                binding.agent_id,
                binding.session_id,
                binding.task_id,
            )
            self._session_task_bindings.setdefault(key, binding.model_copy(deep=True))

    async def list_session_task_ids(self, identity: TaskSessionIdentity) -> list[str]:
        """按创建时间稳定列出一个完整会话身份拥有的全部任务。"""

        identity = TaskSessionIdentity.model_validate(identity.model_dump(mode="python"))
        bindings = [
            binding
            for binding in self._session_task_bindings.values()
            if (
                binding.user_id,
                binding.agent_id,
                binding.session_id,
            ) == (identity.user_id, identity.agent_id, identity.session_id)
        ]
        return [
            binding.task_id
            for binding in sorted(bindings, key=lambda item: (item.created_at, item.task_id))
        ]

    async def task_belongs_to_session(
        self, identity: TaskSessionIdentity, task_id: str
    ) -> bool:
        """只检查四元组绑定，不能因任务真实存在而放行。"""

        identity = TaskSessionIdentity.model_validate(identity.model_dump(mode="python"))
        return (
            identity.user_id,
            identity.agent_id,
            identity.session_id,
            task_id,
        ) in self._session_task_bindings

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

    async def create_root_dataset_revision(self, revision: DatasetRevision) -> DatasetRevision:
        """创建只引用 InputVersion 的根版本；不复制 payload。"""

        async with self._lock:
            task = self._tasks.get(revision.task_id)
            input_version = self._inputs.get(revision.root_input_version_id)
            if task is None or input_version is None:
                raise InfrastructureError("DATASET_REVISION_TASK_MISMATCH", "根版本归属无效")
            if (
                input_version.task_id != revision.task_id
                or input_version.well_id != revision.well_id
            ):
                raise InfrastructureError(
                    "DATASET_REVISION_TASK_MISMATCH", "根输入不属于任务或井"
                )
            if revision.parent_revision_id is not None or revision.change_set_id is not None:
                raise InfrastructureError("INVALID_DATASET_REVISION_CHAIN", "根版本引用无效")
            if revision.sequence != 1 or revision.lineage_sha256 != root_lineage(
                input_version.content_sha256
            ):
                raise InfrastructureError("INVALID_DATASET_REVISION_CHAIN", "根版本摘要或序号无效")
            if revision.created_from_execution_id is not None:
                execution = self._executions.get(revision.created_from_execution_id)
                if execution is None or execution.task_id != revision.task_id:
                    raise InfrastructureError(
                        "EXECUTION_TASK_MISMATCH", "来源执行不属于当前任务"
                    )
            if self._task_dataset_revisions[revision.task_id]:
                raise InfrastructureError("DATASET_ROOT_EXISTS", "任务已存在根数据集版本")
            self._dataset_revisions[revision.dataset_revision_id] = revision.model_copy(deep=True)
            self._task_dataset_revisions[revision.task_id].append(revision.dataset_revision_id)
            return revision.model_copy(deep=True)

    async def create_revision_with_change_set(
        self, revision: DatasetRevision, change_set: DatasetChangeSet
    ) -> tuple[DatasetRevision, DatasetChangeSet]:
        """同一进程锁内原子创建 ChangeSet 与 Child Revision。"""

        async with self._lock:
            parent = self._dataset_revisions.get(revision.parent_revision_id or "")
            task = self._tasks.get(revision.task_id)
            if task is None or parent is None or parent.task_id != revision.task_id:
                raise InfrastructureError("DATASET_REVISION_TASK_MISMATCH", "父版本归属无效")
            if (
                parent.well_id != revision.well_id
                or change_set.task_id != revision.task_id
                or change_set.well_id != revision.well_id
            ):
                raise InfrastructureError(
                    "DATASET_REVISION_TASK_MISMATCH", "版本或 ChangeSet 归属无效"
                )
            if (
                revision.parent_revision_id != change_set.base_revision_id
                or revision.change_set_id != change_set.change_set_id
                or revision.root_input_version_id != parent.root_input_version_id
            ):
                raise InfrastructureError(
                    "INVALID_DATASET_REVISION_CHAIN", "版本 lineage 引用不一致"
                )
            if change_set.content_sha256 != change_set_digest(change_set.curve_changes):
                raise InfrastructureError("INVALID_CHANGE_SET_DIGEST", "ChangeSet 摘要无效")
            if revision.lineage_sha256 != child_lineage(
                parent.lineage_sha256, change_set.content_sha256
            ):
                raise InfrastructureError("INVALID_DATASET_REVISION_CHAIN", "子版本摘要无效")
            if any(
                item.change_set_id == change_set.change_set_id
                for item in self._dataset_revisions.values()
            ):
                raise InfrastructureError(
                    "DATASET_CHANGE_SET_ALREADY_USED", "ChangeSet 已用于其他数据集版本"
                )
            for execution_id in (
                revision.created_from_execution_id,
                change_set.source_execution_id,
            ):
                if execution_id is not None:
                    execution = self._executions.get(execution_id)
                    if execution is None or execution.task_id != revision.task_id:
                        raise InfrastructureError(
                            "EXECUTION_TASK_MISMATCH", "来源执行不属于当前任务"
                        )
            sequences = {
                item.sequence
                for item in self._dataset_revisions.values()
                if item.task_id == revision.task_id
            }
            revision = DatasetRevision.model_validate(
                {**revision.model_dump(mode="python"), "sequence": max(sequences) + 1}
            )
            if (
                revision.dataset_revision_id in self._dataset_revisions
                or change_set.change_set_id in self._dataset_change_sets
            ):
                raise InfrastructureError(
                    "DATASET_REVISION_EXISTS", "数据集版本或 ChangeSet 已存在"
                )
            self._dataset_change_sets[change_set.change_set_id] = change_set.model_copy(deep=True)
            self._dataset_revisions[revision.dataset_revision_id] = revision.model_copy(deep=True)
            self._task_dataset_revisions[revision.task_id].append(revision.dataset_revision_id)
            return revision.model_copy(deep=True), change_set.model_copy(deep=True)

    async def get_dataset_revision(self, revision_id: str) -> DatasetRevision | None:
        revision = self._dataset_revisions.get(revision_id)
        return revision.model_copy(deep=True) if revision is not None else None

    async def list_dataset_revisions(self, task_id: str) -> list[DatasetRevision]:
        return [
            self._dataset_revisions[item].model_copy(deep=True)
            for item in self._task_dataset_revisions.get(task_id, [])
        ]

    async def get_dataset_change_set(self, change_set_id: str) -> DatasetChangeSet | None:
        change_set = self._dataset_change_sets.get(change_set_id)
        return change_set.model_copy(deep=True) if change_set is not None else None

    async def list_dataset_change_sets(self, task_id: str) -> list[DatasetChangeSet]:
        return [
            item.model_copy(deep=True)
            for item in self._dataset_change_sets.values()
            if item.task_id == task_id
        ]

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
        run_mode: ExecutionRunMode = ExecutionRunMode.CONTINUOUS,
    ) -> Execution:
        """锁内分配序号并创建执行，随后才移动当前指针。"""

        async with self._lock:
            task_id = state.task.task_id
            task = self._tasks.get(task_id)
            if task is None:
                raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
            if task.well_id != state.task.well_id:
                raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
            if (
                expected_current_execution_id is not None
                and task.current_execution_id != expected_current_execution_id
            ):
                raise InfrastructureError("STALE_EXECUTION_PLAN", "任务当前版本已变化，请重新规划")
            if input_version_id is not None:
                input_version = self._inputs.get(input_version_id)
                if input_version is None:
                    raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
                if input_version.task_id != task_id:
                    raise InfrastructureError("INPUT_VERSION_TASK_MISMATCH", "输入版本不属于此任务")
            execution_id = state.workflow_execution_id
            if execution_id in self._executions:
                raise InfrastructureError("EXECUTION_EXISTS", "执行标识已存在")
            current = self._executions.get(task.current_execution_id or "")
            if current is not None and current.status in {
                ExecutionStatus.QUEUED, ExecutionStatus.RUNNING
            }:
                raise InfrastructureError("TASK_EXECUTION_ACTIVE", "当前任务已有执行正在处理")
            existing = [self._executions[item].sequence for item in self._task_executions[task_id]]
            next_sequence = max(existing, default=0) + 1 if sequence is None else sequence
            if next_sequence < 1 or next_sequence in existing:
                raise InfrastructureError("EXECUTION_SEQUENCE_EXISTS", "任务执行序号已存在或无效")
            now = utc_now()
            execution = Execution(
                execution_id=execution_id,
                task_id=task_id,
                sequence=next_sequence,
                status=ExecutionStatus.QUEUED,
                run_mode=run_mode,
                state_snapshot=state.model_copy(deep=True),
                trigger_type=trigger_type,
                input_version_id=input_version_id,
                override_snapshot=(override_snapshot or InterpretationOverride()).model_copy(
                    deep=True
                ),
                start_step=start_step,
                source_execution_id=source_execution_id,
                planning_reason=planning_reason,
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

    async def claim_execution(
        self, execution_id: str, worker_id: str, lease_expires_at: datetime
    ) -> bool:
        """同一锁内把 QUEUED 原子转换为带 lease 的 RUNNING。"""

        async with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
            if execution.status != ExecutionStatus.QUEUED:
                return False
            now = utc_now()
            self._executions[execution_id] = Execution.model_validate({
                **execution.model_dump(mode="python"),
                "status": ExecutionStatus.RUNNING,
                "started_at": execution.started_at or now,
                "lease_owner": worker_id,
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            })
            return True

    async def renew_execution_lease(
        self, execution_id: str, worker_id: str, lease_expires_at: datetime
    ) -> bool:
        """只有仍由当前 Worker 持有且未过期的 lease 可以续约。"""

        async with self._lock:
            execution = self._executions.get(execution_id)
            now = utc_now()
            if (
                execution is None
                or execution.status != ExecutionStatus.RUNNING
                or execution.lease_owner != worker_id
                or execution.lease_expires_at is None
                or execution.lease_expires_at <= now
            ):
                return False
            execution.lease_expires_at = lease_expires_at
            execution.updated_at = now
            return True

    async def finish_execution(
        self,
        execution_id: str,
        worker_id: str,
        status: ExecutionStatus,
        error_code: str | None = None,
    ) -> Execution:
        """Worker 只能终结自己持有的 RUNNING；历史终态不可覆盖。"""

        if status not in TERMINAL_EXECUTION_STATUSES:
            raise InfrastructureError("INVALID_EXECUTION_STATUS", "执行只能写入终态")
        async with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
            if execution.status != ExecutionStatus.RUNNING:
                raise InfrastructureError("EXECUTION_NOT_RUNNING", "执行不在运行态")
            if execution.lease_owner != worker_id:
                raise InfrastructureError(
                    "EXECUTION_LEASE_MISMATCH", "执行 lease 不属于当前 Worker"
                )
            now = utc_now()
            if status in {ExecutionStatus.SUCCESS, ExecutionStatus.WARNING} and (
                execution.lease_expires_at is None or execution.lease_expires_at <= now
            ):
                raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
            execution = Execution.model_validate({
                **execution.model_dump(mode="python"),
                "status": status,
                "finished_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "error_code": error_code,
                "updated_at": now,
            })
            self._executions[execution_id] = execution
            task = self._tasks[execution.task_id]
            task.updated_at = now
            if status in {ExecutionStatus.SUCCESS, ExecutionStatus.WARNING} and execution.markdown:
                task.latest_successful_execution_id = execution_id
            return execution.model_copy(deep=True)

    async def pause_execution_for_confirmation(
        self,
        execution_id: str,
        worker_id: str,
        state: InterpretationState,
        markdown: str = "",
    ) -> Execution:
        """同一锁内保存候选结果、释放 lease，并进入等待确认态。"""

        async with self._lock:
            execution = self._executions.get(execution_id)
            task = self._tasks.get(state.task.task_id)
            if execution is None or task is None or execution.task_id != task.task_id:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
            if task.current_execution_id != execution_id:
                raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不能进入确认态")
            if execution.run_mode != ExecutionRunMode.STAGED_CONFIRMATION:
                raise InfrastructureError("INVALID_EXECUTION_MODE", "执行不是分阶段确认模式")
            if execution.status != ExecutionStatus.RUNNING or execution.lease_owner != worker_id:
                raise InfrastructureError("EXECUTION_LEASE_MISMATCH", "执行不由当前 Worker 持有")
            if execution.lease_expires_at is None or execution.lease_expires_at <= utc_now():
                raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
            waiting = [
                run for run in state.stage_runs
                if run.execution_id == execution_id
                and run.status == StageRunStatus.WAITING_CONFIRM
                and run.validity == StageValidity.CURRENT
            ]
            if len(waiting) != 1 or any(
                run.execution_id == execution_id and run.status == StageRunStatus.RUNNING
                for run in state.stage_runs
            ):
                raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "阶段确认快照无效")
            now = utc_now()
            execution = Execution.model_validate({
                **execution.model_dump(mode="python"),
                "status": ExecutionStatus.WAITING_CONFIRMATION,
                "state_snapshot": state,
                "markdown": markdown,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            })
            self._executions[execution_id] = execution
            self._states[task.task_id] = state.model_copy(deep=True)
            self.reports[task.task_id] = markdown
            task.updated_at = now
            return execution.model_copy(deep=True)

    def _confirm_waiting_run(
        self,
        execution: Execution,
        stage: InterpretationStage,
        expected_stage_run_id: str,
        actor: str,
    ) -> InterpretationState:
        """锁内按预期 ID 确认唯一当前 StageRun，防止双击覆盖。"""

        state = execution.state_snapshot.model_copy(deep=True)
        matches = [
            (index, run) for index, run in enumerate(state.stage_runs)
            if run.task_id == execution.task_id
            and run.execution_id == execution.execution_id
            and run.stage == stage
        ]
        if len(matches) != 1:
            raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "等待确认阶段不存在")
        index, run = matches[0]
        if (
            run.id != expected_stage_run_id
            or run.status != StageRunStatus.WAITING_CONFIRM
            or run.validity != StageValidity.CURRENT
        ):
            raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "阶段已变化，请刷新后重试")
        state.stage_runs[index] = confirm_stage(run, actor=actor)
        state.updated_at = utc_now()
        return state

    async def confirm_stage_and_requeue(
        self,
        task_id: str,
        execution_id: str,
        stage: InterpretationStage,
        expected_stage_run_id: str,
        actor: str,
    ) -> Execution:
        """确认非报告阶段并把同一 Execution 原子放回队列。"""

        if stage == InterpretationStage.REPORT:
            raise InfrastructureError("INVALID_STAGE", "报告阶段应使用最终确认入口")
        async with self._lock:
            task = self._tasks.get(task_id)
            execution = self._executions.get(execution_id)
            if task is None or execution is None or execution.task_id != task_id:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
            if task.current_execution_id != execution_id:
                raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不能确认")
            if execution.status != ExecutionStatus.WAITING_CONFIRMATION:
                raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "执行不在等待确认态")
            state = self._confirm_waiting_run(
                execution, stage, expected_stage_run_id, actor
            )
            now = utc_now()
            updated = Execution.model_validate({
                **execution.model_dump(mode="python"),
                "status": ExecutionStatus.QUEUED,
                "state_snapshot": state,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            })
            self._executions[execution_id] = updated
            self._states[task_id] = state.model_copy(deep=True)
            task.updated_at = now
            return updated.model_copy(deep=True)

    async def confirm_final_stage_and_finish(
        self,
        task_id: str,
        execution_id: str,
        expected_stage_run_id: str,
        actor: str,
    ) -> Execution:
        """确认报告候选并原子形成 Execution 终态和最新成功指针。"""

        async with self._lock:
            task = self._tasks.get(task_id)
            execution = self._executions.get(execution_id)
            if task is None or execution is None or execution.task_id != task_id:
                raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
            if task.current_execution_id != execution_id:
                raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不能确认")
            if execution.status != ExecutionStatus.WAITING_CONFIRMATION:
                raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "执行不在等待确认态")
            if not execution.markdown:
                raise InfrastructureError("REPORT_NOT_READY", "报告候选尚未生成")
            state = self._confirm_waiting_run(
                execution, InterpretationStage.REPORT, expected_stage_run_id, actor
            )
            status = execution_status_from_state(state.completed_status())
            now = utc_now()
            updated = Execution.model_validate({
                **execution.model_dump(mode="python"),
                "status": status,
                "state_snapshot": state,
                "finished_at": now,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            })
            self._executions[execution_id] = updated
            self._states[task_id] = state.model_copy(deep=True)
            self.reports[task_id] = execution.markdown
            task.latest_successful_execution_id = execution_id
            task.updated_at = now
            return updated.model_copy(deep=True)

    async def recover_expired_executions(self, now: datetime) -> list[str]:
        """过期 RUNNING 标记失败；不透明重放原 Execution。"""

        async with self._lock:
            expired = [
                item for item in self._executions.values()
                if item.status == ExecutionStatus.RUNNING
                and item.lease_expires_at is not None
                and item.lease_expires_at <= now
            ]
            for execution in expired:
                self._executions[execution.execution_id] = Execution.model_validate({
                    **execution.model_dump(mode="python"),
                    "status": ExecutionStatus.FAILED,
                    "finished_at": now,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "error_code": "WORKER_LEASE_EXPIRED",
                    "updated_at": now,
                })
            return [item.execution_id for item in expired]

    async def create_tool_run(self, run: ToolRun) -> ToolRun:
        """只接受已创建 Execution 的真实调用，返回与历史存储隔离的副本。"""

        async with self._lock:
            run = ToolRun.model_validate(run.model_dump(mode="python"))
            execution = self._executions.get(run.execution_id)
            if execution is None or execution.task_id != run.task_id:
                raise InfrastructureError("TOOL_RUN_EXECUTION_MISMATCH", "工具调用不属于该执行")
            if run.status != ToolRunStatus.RUNNING:
                raise InfrastructureError("TOOL_RUN_INVALID_STATUS", "工具调用必须以运行态创建")
            if run.tool_run_id in self._tool_runs:
                raise InfrastructureError("TOOL_RUN_EXISTS", "工具调用标识已存在")
            self._tool_runs[run.tool_run_id] = run.model_copy(deep=True)
            self._execution_tool_runs.setdefault(run.execution_id, []).append(run.tool_run_id)
            return run.model_copy(deep=True)

    async def finish_tool_run(
        self,
        tool_run_id: str,
        *,
        status: ToolRunStatus,
        source: str,
        output_snapshot: JsonObject,
        error_code: str | None = None,
        error_message: str | None = None,
        source_external_call_id: str | None = None,
    ) -> ToolRun:
        """终态只写一次，不允许改写历史调用结果。"""

        async with self._lock:
            current = self._tool_runs.get(tool_run_id)
            if current is None:
                raise InfrastructureError("TOOL_RUN_NOT_FOUND", "工具调用不存在")
            if current.status != ToolRunStatus.RUNNING:
                raise InfrastructureError("TOOL_RUN_ALREADY_FINISHED", "工具调用已结束")
            if status == ToolRunStatus.RUNNING:
                raise InfrastructureError("TOOL_RUN_INVALID_STATUS", "结束调用必须使用终态")
            finished = ToolRun.model_validate({
                **current.model_dump(mode="python"),
                "status": status,
                "source": source,
                "output_snapshot": output_snapshot,
                "finished_at": utc_now(),
                "error_code": error_code,
                "error_message": error_message,
                "source_external_call_id": source_external_call_id,
            })
            self._tool_runs[tool_run_id] = finished
            return finished.model_copy(deep=True)

    async def get_tool_run(self, tool_run_id: str) -> ToolRun | None:
        run = self._tool_runs.get(tool_run_id)
        return run.model_copy(deep=True) if run is not None else None

    async def list_tool_runs(self, execution_id: str) -> list[ToolRun]:
        """创建顺序是本次 Workflow Tool 调用的稳定顺序。"""

        runs = (self._tool_runs[item] for item in self._execution_tool_runs.get(execution_id, []))
        return [run.model_copy(deep=True) for run in sorted(
            runs, key=lambda item: (item.started_at, item.tool_run_id)
        )]

    async def create_company_provider_call(
        self, call: CompanyProviderCall
    ) -> CompanyProviderCall:
        """核对 Task、Execution 和 InputVersion 后保存真实调用运行态。"""

        async with self._lock:
            call = CompanyProviderCall.model_validate(call.model_dump(mode="python"))
            execution = self._executions.get(call.execution_id)
            input_version = self._inputs.get(call.input_version_id)
            if (
                execution is None
                or execution.task_id != call.task_id
                or execution.input_version_id != call.input_version_id
                or input_version is None
                or input_version.task_id != call.task_id
            ):
                raise InfrastructureError(
                    "COMPANY_RESULT_VERSION_MISMATCH", "公司调用不属于当前执行及输入版本"
                )
            if call.external_call_id in self._company_provider_calls:
                raise InfrastructureError("COMPANY_PROVIDER_CALL_EXISTS", "公司调用标识已存在")
            if any(
                item.task_id == call.task_id
                and item.execution_id == call.execution_id
                and item.input_version_id == call.input_version_id
                and item.provider_operation == call.provider_operation
                for item in self._company_provider_calls.values()
            ):
                raise InfrastructureError(
                    "COMPANY_PROVIDER_CALL_EXISTS", "同一执行和输入版本已存在该公司调用"
                )
            self._company_provider_calls[call.external_call_id] = call.model_copy(deep=True)
            return call.model_copy(deep=True)

    async def finish_company_provider_call(
        self,
        external_call_id: str,
        *,
        status: CompanyProviderCallStatus,
        normalized_result: JsonObject,
        error_code: str | None = None,
    ) -> CompanyProviderCall:
        """在同一进程锁内一次性结束调用，失败事实不保存响应正文。"""

        async with self._lock:
            current = self._company_provider_calls.get(external_call_id)
            if current is None:
                raise InfrastructureError("COMPANY_PROVIDER_CALL_NOT_FOUND", "公司调用不存在")
            if current.status != CompanyProviderCallStatus.RUNNING:
                raise InfrastructureError("COMPANY_PROVIDER_CALL_FINISHED", "公司调用已经结束")
            finished = CompanyProviderCall.model_validate({
                **current.model_dump(mode="python"),
                "status": status,
                "normalized_result": normalized_result,
                "finished_at": utc_now(),
                "error_code": error_code,
            })
            self._company_provider_calls[external_call_id] = finished
            return finished.model_copy(deep=True)

    async def get_company_provider_call(
        self, external_call_id: str
    ) -> CompanyProviderCall | None:
        call = self._company_provider_calls.get(external_call_id)
        return call.model_copy(deep=True) if call is not None else None

    async def list_company_provider_calls(
        self,
        task_id: str,
        *,
        execution_id: str | None = None,
        operation: CompanyProviderOperation | None = None,
    ) -> list[CompanyProviderCall]:
        calls = [
            call
            for call in self._company_provider_calls.values()
            if call.task_id == task_id
            and (execution_id is None or call.execution_id == execution_id)
            and (operation is None or call.provider_operation == operation)
        ]
        return [
            call.model_copy(deep=True)
            for call in sorted(calls, key=lambda item: (item.started_at, item.provider_call_id))
        ]

    async def get_execution_report(self, execution_id: str) -> str | None:
        execution = self._executions.get(execution_id)
        return execution.markdown if execution is not None else None

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
            if execution.status != ExecutionStatus.RUNNING:
                raise InfrastructureError("EXECUTION_NOT_RUNNING", "执行不在运行态")
            if (
                execution.lease_expires_at is None or execution.lease_expires_at <= utc_now()
            ):
                raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
            execution.state_snapshot = state.model_copy(deep=True)
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
                status=ExecutionStatus.QUEUED,
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
            if execution.error_code == "WORKER_LEASE_EXPIRED":
                raise InfrastructureError("EXECUTION_NOT_RUNNING", "过期执行不可再写入")
            if execution.status == ExecutionStatus.RUNNING and (
                execution.lease_expires_at is None or execution.lease_expires_at <= utc_now()
            ):
                raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
            now = utc_now()
            execution.state_snapshot = state.model_copy(deep=True)
            execution.markdown = markdown
            execution.updated_at = now
            self._states[task_id] = state.model_copy(deep=True)
            self.reports[task_id] = markdown
            task.updated_at = now

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None
