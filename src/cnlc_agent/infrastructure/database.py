"""异步 PostgreSQL 仓库；Execution 独立存历史，Task 行保留当前快照兼容视图。"""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cnlc_agent.domain.enums import StepStatus
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.execution import Execution, ExecutionTrigger, InterpretationTask
from cnlc_agent.domain.inputs import (
    InputSource,
    InterpretationInputVersion,
    fixture_digest,
)
from cnlc_agent.domain.models import JsonObject, MockFixture, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRun, ToolRunStatus


class Base(DeclarativeBase):
    """SQLAlchemy 声明式模型基类。"""

    pass


class TaskRow(Base):
    """持续任务；snapshot/markdown 是旧接口读取当前执行的兼容视图。"""

    __tablename__ = "interpretation_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    well_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    snapshot: Mapped[JsonObject] = mapped_column(JSONB)
    markdown: Mapped[str] = mapped_column(Text)
    current_execution_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_input_version_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_successful_execution_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InputVersionRow(Base):
    """已校验井资料的持久版本；不保存原始浏览器附件或临时文件路径。"""

    __tablename__ = "interpretation_input_version"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_input_version_task_sequence"),
    )

    input_version_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    well_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionRow(Base):
    """每次运行的完整快照与报告，不被后续执行覆盖。"""

    __tablename__ = "interpretation_execution"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_execution_task_sequence"),)

    execution_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_snapshot: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    input_version_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_input_version.input_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    override_snapshot: Mapped[JsonObject] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolRunRow(Base):
    """单次 Execution 的专业工具审计行，索引支持按执行顺序查询。"""

    __tablename__ = "interpretation_tool_run"

    tool_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("interpretation_task.task_id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_execution.execution_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str] = mapped_column(String(8), nullable=False)
    tool_code: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    input_snapshot: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    output_snapshot: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    source_external_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


def _as_tool_run(row: ToolRunRow) -> ToolRun:
    """读回持久审计时重新校验生命周期与字段。"""

    return ToolRun(
        tool_run_id=row.tool_run_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        step_id=row.step_id,  # type: ignore[arg-type]
        tool_code=row.tool_code,
        status=ToolRunStatus(row.status),
        execution_mode=ToolExecutionMode(row.execution_mode),
        source=row.source,
        input_snapshot=row.input_snapshot,
        output_snapshot=row.output_snapshot,
        source_external_call_id=row.source_external_call_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
        error_message=row.error_message,
    )


def _as_execution(row: ExecutionRow) -> Execution:
    """读回时重新校验持久化快照与执行标识的一致性。"""

    return Execution(
        execution_id=row.execution_id,
        task_id=row.task_id,
        sequence=row.sequence,
        status=StepStatus(row.status),
        state_snapshot=InterpretationState.model_validate(row.state_snapshot),
        markdown=row.markdown,
        trigger_type=row.trigger_type,  # type: ignore[arg-type]
        input_version_id=row.input_version_id,
        override_snapshot=InterpretationOverride.model_validate(row.override_snapshot),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _as_input_version(row: InputVersionRow) -> InterpretationInputVersion:
    """反序列化时重新验证规范化输入及内容摘要。"""

    return InterpretationInputVersion(
        input_version_id=row.input_version_id,
        task_id=row.task_id,
        well_id=row.well_id,
        sequence=row.sequence,
        source_type=row.source_type,  # type: ignore[arg-type]
        content_sha256=row.content_sha256,
        payload=MockFixture.model_validate(row.payload),
        created_at=row.created_at,
    )


class PostgreSQLTaskRepository:
    """TaskRepository 的 PostgreSQL 实现，序号在任务行锁内分配。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_tool_run(self, run: ToolRun) -> ToolRun:
        """核对执行与任务归属，在单事务中创建 RUNNING 审计行。"""

        try:
            run = ToolRun.model_validate(run.model_dump(mode="python"))
            if run.status != ToolRunStatus.RUNNING:
                raise InfrastructureError("TOOL_RUN_INVALID_STATUS", "工具调用必须以运行态创建")
            async with self.sessions.begin() as session:
                execution = await session.get(ExecutionRow, run.execution_id)
                if execution is None or execution.task_id != run.task_id:
                    raise InfrastructureError("TOOL_RUN_EXECUTION_MISMATCH", "工具调用不属于该执行")
                session.add(ToolRunRow(
                    tool_run_id=run.tool_run_id,
                    task_id=run.task_id,
                    execution_id=run.execution_id,
                    step_id=run.step_id.value,
                    tool_code=run.tool_code,
                    status=run.status.value,
                    execution_mode=run.execution_mode.value,
                    source=run.source,
                    input_snapshot=run.input_snapshot,
                    output_snapshot=run.output_snapshot,
                    source_external_call_id=run.source_external_call_id,
                    started_at=run.started_at,
                    finished_at=None,
                    error_code=None,
                    error_message=None,
                ))
            return run.model_copy(deep=True)
        except IntegrityError:
            raise InfrastructureError("TOOL_RUN_EXISTS", "工具调用标识已存在") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库工具审计创建失败") from None

    async def finish_tool_run(
        self,
        tool_run_id: str,
        *,
        status: ToolRunStatus,
        source: str,
        output_snapshot: JsonObject,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ToolRun:
        """行锁保障一次终结；审计记录的身份、起点和输入不可改写。"""

        try:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(ToolRunRow)
                    .where(ToolRunRow.tool_run_id == tool_run_id)
                    .with_for_update()
                )
                if row is None:
                    raise InfrastructureError("TOOL_RUN_NOT_FOUND", "工具调用不存在")
                if row.status != ToolRunStatus.RUNNING.value:
                    raise InfrastructureError("TOOL_RUN_ALREADY_FINISHED", "工具调用已结束")
                if status == ToolRunStatus.RUNNING:
                    raise InfrastructureError("TOOL_RUN_INVALID_STATUS", "结束调用必须使用终态")
                finished = ToolRun.model_validate({
                    **_as_tool_run(row).model_dump(mode="python"),
                    "status": status,
                    "source": source,
                    "output_snapshot": output_snapshot,
                    "finished_at": utc_now(),
                    "error_code": error_code,
                    "error_message": error_message,
                })
                row.status = finished.status.value
                row.source = finished.source
                row.output_snapshot = finished.output_snapshot
                row.finished_at = finished.finished_at
                row.error_code = finished.error_code
                row.error_message = finished.error_message
            return finished
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库工具审计结束失败") from None

    async def get_tool_run(self, tool_run_id: str) -> ToolRun | None:
        try:
            async with self.sessions() as session:
                row = await session.get(ToolRunRow, tool_run_id)
                return _as_tool_run(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_TOOL_RUN", "数据库工具审计结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库工具审计读取失败") from None

    async def list_tool_runs(self, execution_id: str) -> list[ToolRun]:
        try:
            async with self.sessions() as session:
                rows = (await session.scalars(
                    select(ToolRunRow).where(ToolRunRow.execution_id == execution_id)
                    .order_by(ToolRunRow.started_at, ToolRunRow.tool_run_id)
                )).all()
                return [_as_tool_run(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_TOOL_RUN", "数据库工具审计结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "DATABASE_READ_FAILED", "数据库工具审计列表读取失败"
            ) from None

    async def get_execution_report(self, execution_id: str) -> str | None:
        """历史报告直接读取 Execution.markdown，不依赖当前任务视图。"""

        try:
            async with self.sessions() as session:
                result = await session.scalar(
                    select(ExecutionRow.markdown).where(ExecutionRow.execution_id == execution_id)
                )
                return str(result) if result is not None else None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库执行报告读取失败") from None

    async def create_task(self, state: InterpretationState) -> None:
        """创建持续任务，保留旧版非空快照列。"""

        try:
            async with self.sessions.begin() as session:
                session.add(
                    TaskRow(
                        task_id=state.task.task_id,
                        well_id=state.task.well_id,
                        status=state.status.value,
                        snapshot=state.model_dump(mode="json"),
                        markdown="",
                        created_at=state.created_at,
                        updated_at=state.updated_at,
                    )
                )
        except IntegrityError:
            raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库任务创建失败") from None

    async def get_task(self, task_id: str) -> InterpretationTask | None:
        """读取任务元信息；不把一次运行快照误作持续任务实体。"""

        try:
            async with self.sessions() as session:
                row = await session.get(TaskRow, task_id)
                if row is None:
                    return None
                return InterpretationTask(
                    task_id=row.task_id,
                    well_id=row.well_id,
                    current_execution_id=row.current_execution_id,
                    current_input_version_id=row.current_input_version_id,
                    latest_successful_execution_id=row.latest_successful_execution_id,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
        except ValidationError:
            raise InfrastructureError("INVALID_STORED_TASK", "数据库任务结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库任务读取失败") from None

    async def create_input_version(
        self, task_id: str, fixture: MockFixture, source_type: InputSource = "UPLOAD"
    ) -> InterpretationInputVersion:
        """任务行锁内分配版本号；输入插入成功后移动当前输入指针。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == task_id).with_for_update()
                )
                if task is None:
                    raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
                if task.well_id != fixture.well.well_id:
                    raise InfrastructureError("TASK_WELL_MISMATCH", "输入井号与任务不一致")
                last_sequence = await session.scalar(
                    select(func.max(InputVersionRow.sequence)).where(
                        InputVersionRow.task_id == task_id
                    )
                )
                version = InterpretationInputVersion(
                    task_id=task_id,
                    well_id=task.well_id,
                    sequence=(last_sequence or 0) + 1,
                    source_type=source_type,
                    content_sha256=fixture_digest(fixture),
                    payload=fixture,
                )
                row = InputVersionRow(
                    input_version_id=version.input_version_id,
                    task_id=task_id,
                    well_id=task.well_id,
                    sequence=version.sequence,
                    source_type=version.source_type,
                    content_sha256=version.content_sha256,
                    payload=fixture.model_dump(mode="json"),
                    created_at=version.created_at,
                )
                session.add(row)
                await session.flush()
                task.current_input_version_id = row.input_version_id
                task.updated_at = utc_now()
            return version.model_copy(deep=True)
        except IntegrityError:
            raise InfrastructureError(
                "INPUT_VERSION_CONFLICT", "输入版本标识或序号已存在"
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库输入版本创建失败") from None

    async def get_input_version(self, input_version_id: str) -> InterpretationInputVersion | None:
        """读取并校验持久化输入快照及摘要。"""

        try:
            async with self.sessions() as session:
                row = await session.get(InputVersionRow, input_version_id)
                return _as_input_version(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_INPUT", "数据库输入版本结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库输入版本读取失败") from None

    async def list_input_versions(self, task_id: str) -> list[InterpretationInputVersion]:
        """按序号返回任务的全部历史输入版本。"""

        try:
            async with self.sessions() as session:
                rows = (await session.scalars(
                    select(InputVersionRow)
                    .where(InputVersionRow.task_id == task_id)
                    .order_by(InputVersionRow.sequence)
                )).all()
                return [_as_input_version(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_INPUT", "数据库输入版本结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "DATABASE_READ_FAILED", "数据库输入版本列表读取失败"
            ) from None

    async def create_execution(
        self,
        state: InterpretationState,
        trigger_type: ExecutionTrigger = "RERUN",
        sequence: int | None = None,
        input_version_id: str | None = None,
        override_snapshot: InterpretationOverride | None = None,
    ) -> Execution:
        """任务行加锁后分配序号；插入成功才更新当前指针。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == state.task.task_id).with_for_update()
                )
                if task is None:
                    raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
                if task.well_id != state.task.well_id:
                    raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
                if input_version_id is not None:
                    input_row = await session.get(InputVersionRow, input_version_id)
                    if input_row is None:
                        raise InfrastructureError("INPUT_VERSION_NOT_FOUND", "输入版本不存在")
                    if input_row.task_id != state.task.task_id:
                        raise InfrastructureError(
                            "INPUT_VERSION_TASK_MISMATCH", "输入版本不属于此任务"
                        )
                if await session.get(ExecutionRow, state.workflow_execution_id) is not None:
                    raise InfrastructureError("EXECUTION_EXISTS", "执行标识已存在")
                last_sequence = await session.scalar(
                    select(func.max(ExecutionRow.sequence)).where(
                        ExecutionRow.task_id == state.task.task_id
                    )
                )
                next_sequence = (last_sequence or 0) + 1 if sequence is None else sequence
                if next_sequence < 1 or (
                    sequence is not None
                    and await session.scalar(
                        select(ExecutionRow.execution_id).where(
                            ExecutionRow.task_id == state.task.task_id,
                            ExecutionRow.sequence == sequence,
                        )
                    )
                    is not None
                ):
                    raise InfrastructureError(
                        "EXECUTION_SEQUENCE_EXISTS", "任务执行序号已存在或无效"
                    )
                now = utc_now()
                row = ExecutionRow(
                    execution_id=state.workflow_execution_id,
                    task_id=state.task.task_id,
                    sequence=next_sequence,
                    status=state.status.value,
                    state_snapshot=state.model_dump(mode="json"),
                    markdown="",
                    trigger_type=trigger_type,
                    input_version_id=input_version_id,
                    override_snapshot=(override_snapshot or InterpretationOverride()).model_dump(
                        mode="json"
                    ),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
                task.current_execution_id = row.execution_id
                task.status = row.status
                task.snapshot = row.state_snapshot
                task.markdown = ""
                task.updated_at = now
                execution = _as_execution(row)
            return execution
        except IntegrityError:
            # task_id + sequence 有数据库唯一约束，防御非合作写入者和并发冲突。
            raise InfrastructureError("EXECUTION_CONFLICT", "执行标识或序号已存在") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库执行创建失败") from None

    async def get_execution(self, execution_id: str) -> Execution | None:
        try:
            async with self.sessions() as session:
                row = await session.get(ExecutionRow, execution_id)
                return _as_execution(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_EXECUTION", "数据库执行结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库执行读取失败") from None

    async def list_executions(self, task_id: str) -> list[Execution]:
        try:
            async with self.sessions() as session:
                rows = (await session.scalars(
                    select(ExecutionRow)
                    .where(ExecutionRow.task_id == task_id)
                    .order_by(ExecutionRow.sequence)
                )).all()
                return [_as_execution(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_EXECUTION", "数据库执行结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库执行列表读取失败") from None

    async def save_execution_state(self, state: InterpretationState) -> None:
        """更新当前执行的检查点；后续执行不得污染历史版本。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == state.task.task_id).with_for_update()
                )
                row = await session.get(ExecutionRow, state.workflow_execution_id)
                if task is None or row is None or row.task_id != state.task.task_id:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
                if task.current_execution_id != row.execution_id:
                    raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不可写入当前快照")
                if task.well_id != state.task.well_id:
                    raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
                payload = state.model_dump(mode="json")
                now = utc_now()
                row.state_snapshot = payload
                row.status = state.status.value
                row.updated_at = now
                task.snapshot = payload
                task.status = row.status
                task.updated_at = now
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库状态保存失败") from None

    async def save_execution_report(self, execution_id: str, markdown: str) -> None:
        """版本报告是事实来源；Task.markdown 只是当前版本兼容视图。"""

        try:
            async with self.sessions.begin() as session:
                row = await session.get(ExecutionRow, execution_id)
                if row is None:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == row.task_id).with_for_update()
                )
                if task is None or task.current_execution_id != execution_id:
                    raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不可改写报告")
                now = utc_now()
                row.markdown = markdown
                row.updated_at = now
                task.markdown = markdown
                task.updated_at = now
                if markdown and row.status in {StepStatus.SUCCESS.value, StepStatus.WARNING.value}:
                    task.latest_successful_execution_id = execution_id
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库报告保存失败") from None

    async def set_current_execution(self, task_id: str, execution_id: str) -> None:
        """只接受属于该 Task 的已持久化执行。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == task_id).with_for_update()
                )
                if task is None:
                    raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
                row = await session.get(ExecutionRow, execution_id)
                if row is None or row.task_id != task_id:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
                if task.current_execution_id is not None:
                    current = await session.get(ExecutionRow, task.current_execution_id)
                    if current is not None and row.sequence < current.sequence:
                        raise InfrastructureError(
                            "EXECUTION_NOT_LATEST", "不能将历史执行设为当前版本"
                        )
                task.current_execution_id = execution_id
                task.status = row.status
                task.snapshot = row.state_snapshot
                task.markdown = row.markdown
                task.updated_at = utc_now()
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库当前执行切换失败") from None

    async def create(self, state: InterpretationState) -> None:
        """兼容旧入口；首个 Task/Execution 在一个事务中建立。"""

        try:
            async with self.sessions.begin() as session:
                if await session.get(TaskRow, state.task.task_id) is not None:
                    raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务")
                if await session.get(ExecutionRow, state.workflow_execution_id) is not None:
                    raise InfrastructureError("EXECUTION_EXISTS", "执行标识已存在")
                payload = state.model_dump(mode="json")
                task = TaskRow(
                    task_id=state.task.task_id,
                    well_id=state.task.well_id,
                    status=state.status.value,
                    snapshot=payload,
                    markdown="",
                    created_at=state.created_at,
                    updated_at=state.updated_at,
                )
                session.add(task)
                await session.flush()
                session.add(
                    ExecutionRow(
                        execution_id=state.workflow_execution_id,
                        task_id=state.task.task_id,
                        sequence=1,
                        status=state.status.value,
                        state_snapshot=payload,
                        markdown="",
                        trigger_type="INITIAL",
                        input_version_id=None,
                        override_snapshot={},
                        created_at=state.created_at,
                        updated_at=state.updated_at,
                    )
                )
                await session.flush()
                task.current_execution_id = state.workflow_execution_id
        except IntegrityError:
            if await self.get_task(state.task.task_id) is not None:
                raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务") from None
            raise InfrastructureError("EXECUTION_EXISTS", "执行标识已存在") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库任务创建失败") from None

    async def save(self, state: InterpretationState, markdown: str) -> None:
        """兼容旧接口；快照、报告和成功指针在同一事务内更新。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == state.task.task_id).with_for_update()
                )
                row = await session.get(ExecutionRow, state.workflow_execution_id)
                if task is None or row is None or row.task_id != state.task.task_id:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行尚未创建")
                if task.current_execution_id != row.execution_id:
                    raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不可写入当前快照")
                if task.well_id != state.task.well_id:
                    raise InfrastructureError("TASK_WELL_MISMATCH", "任务井号不一致")
                now = utc_now()
                payload = state.model_dump(mode="json")
                row.state_snapshot = payload
                row.status = state.status.value
                row.markdown = markdown
                row.updated_at = now
                task.snapshot = payload
                task.status = row.status
                task.markdown = markdown
                task.updated_at = now
                if markdown and row.status in {StepStatus.SUCCESS.value, StepStatus.WARNING.value}:
                    task.latest_successful_execution_id = row.execution_id
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库状态与报告保存失败") from None

    async def get(self, task_id: str) -> InterpretationState | None:
        """旧接口返回当前 Execution 的快照。"""

        try:
            async with self.sessions() as session:
                payload = await session.scalar(
                    select(TaskRow.snapshot).where(TaskRow.task_id == task_id)
                )
            return InterpretationState.model_validate(payload) if payload is not None else None
        except ValidationError:
            raise InfrastructureError("INVALID_STORED_STATE", "数据库状态结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库状态读取失败") from None

    async def get_report(self, task_id: str) -> str | None:
        """旧接口返回当前 Execution 的 Markdown。"""

        try:
            async with self.sessions() as session:
                result = await session.scalar(
                    select(TaskRow.markdown).where(TaskRow.task_id == task_id)
                )
            return str(result) if result is not None else None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库报告读取失败") from None
