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
from cnlc_agent.domain.models import JsonObject, utc_now
from cnlc_agent.domain.state import InterpretationState


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
    latest_successful_execution_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PostgreSQLTaskRepository:
    """TaskRepository 的 PostgreSQL 实现，序号在任务行锁内分配。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

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
                    latest_successful_execution_id=row.latest_successful_execution_id,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
        except ValidationError:
            raise InfrastructureError("INVALID_STORED_TASK", "数据库任务结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库任务读取失败") from None

    async def create_execution(
        self,
        state: InterpretationState,
        trigger_type: ExecutionTrigger = "RERUN",
        sequence: int | None = None,
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
