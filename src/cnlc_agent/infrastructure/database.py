"""异步 PostgreSQL 仓库；Execution 独立存历史，Task 行保留当前快照兼容视图。"""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cnlc_agent.domain.artifacts import Artifact, ArtifactKind, GdsxDatasetManifest
from cnlc_agent.domain.company_provider import (
    CompanyProviderCall,
    CompanyProviderCallStatus,
    CompanyProviderOperation,
)
from cnlc_agent.domain.dataset_revision import (
    DatasetChangeSet,
    DatasetChangeType,
    DatasetCurveChange,
    DatasetRevision,
    change_set_digest,
    child_lineage,
    root_lineage,
)
from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import InfrastructureError
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
    InputPayloadKind,
    InputSource,
    InterpretationInputVersion,
    fixture_digest,
)
from cnlc_agent.domain.models import JsonObject, MockFixture, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.domain.stages import (
    InterpretationStage,
    StageRunStatus,
    StageValidity,
    confirm_stage,
)
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


class SessionTaskBindingRow(Base):
    """AgentScope 会话对解释任务的持久归属；不是通用权限或 RBAC 表。"""

    __tablename__ = "interpretation_session_task_binding"
    __table_args__ = (
        Index(
            "ix_session_task_binding_session",
            "user_id",
            "agent_id",
            "session_id",
        ),
        Index("ix_session_task_binding_task_id", "task_id"),
    )

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    payload_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=InputPayloadKind.FIXTURE.value
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    source_artifact_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_artifact.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    dataset_manifest: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DatasetRevisionRow(Base):
    """不可变 Dataset lineage metadata；不保存物化 RawData。"""

    __tablename__ = "interpretation_dataset_revision"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_dataset_revision_task_sequence"),
        UniqueConstraint("change_set_id", name="uq_dataset_revision_change_set_id"),
    )

    dataset_revision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    well_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    root_input_version_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_input_version.input_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parent_revision_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_dataset_revision.dataset_revision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    change_set_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_dataset_change_set.change_set_id", ondelete="RESTRICT"),
        nullable=True,
    )
    lineage_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_from_execution_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_execution.execution_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DatasetChangeSetRow(Base):
    """稀疏曲线修改；payload 只保存 DatasetCurveChange 列表。"""

    __tablename__ = "interpretation_dataset_change_set"

    change_set_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    well_id: Mapped[str] = mapped_column(String(64), nullable=False)
    base_revision_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_dataset_revision.dataset_revision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[list[JsonObject]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    source_execution_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_execution.execution_id", ondelete="RESTRICT"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    run_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExecutionRunMode.CONTINUOUS.value,
        server_default=ExecutionRunMode.CONTINUOUS.value,
    )
    state_snapshot: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    input_version_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_input_version.input_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    override_snapshot: Mapped[JsonObject] = mapped_column(JSONB, nullable=False, default=dict)
    start_step: Mapped[str | None] = mapped_column(String(8), nullable=True)
    source_execution_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    planning_reason: Mapped[str] = mapped_column(String(32), nullable=False, default="INITIAL")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
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


class CompanyProviderCallRow(Base):
    """真实公司物理调用的可恢复规范化事实，不存文件字节或响应正文。"""

    __tablename__ = "interpretation_company_provider_call"
    __table_args__ = (
        Index(
            "ix_company_provider_call_execution_operation",
            "execution_id",
            "provider_operation",
        ),
        CheckConstraint(
            "provider_operation IN ('analysis', 'preprocessing', 'interpretation', 'report')",
            name="ck_company_provider_call_operation",
        ),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCESS', 'UNKNOWN', 'FAILED')",
            name="ck_company_provider_call_status",
        ),
        UniqueConstraint(
            "task_id",
            "execution_id",
            "input_version_id",
            "provider_operation",
            name="uq_company_provider_call_identity_operation",
        ),
    )

    provider_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    external_call_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    task_id: Mapped[str] = mapped_column(
        Text, ForeignKey("interpretation_task.task_id", ondelete="CASCADE"), nullable=False
    )
    execution_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_execution.execution_id", ondelete="CASCADE"),
        nullable=False,
    )
    input_version_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_input_version.input_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_summary: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    normalized_result: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ArtifactRow(Base):
    """GDSX Artifact 元数据；文件字节只进入 ArtifactStore。"""

    __tablename__ = "interpretation_artifact"

    artifact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    well_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_artifact_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_artifact.artifact_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_from_execution_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_execution.execution_id", ondelete="RESTRICT"),
        nullable=True,
    )
    provider_call_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("interpretation_company_provider_call.provider_call_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


def _as_company_provider_call(row: CompanyProviderCallRow) -> CompanyProviderCall:
    """读回时重新执行生命周期、敏感字段和体积校验。"""

    return CompanyProviderCall(
        provider_call_id=row.provider_call_id,
        external_call_id=row.external_call_id,
        task_id=row.task_id,
        execution_id=row.execution_id,
        input_version_id=row.input_version_id,
        provider_operation=CompanyProviderOperation(row.provider_operation),
        status=CompanyProviderCallStatus(row.status),
        request_summary=row.request_summary,
        normalized_result=row.normalized_result,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
    )


def _as_execution(row: ExecutionRow) -> Execution:
    """读回时重新校验持久化快照与执行标识的一致性。"""

    return Execution(
        execution_id=row.execution_id,
        task_id=row.task_id,
        sequence=row.sequence,
        status=ExecutionStatus(row.status),
        run_mode=ExecutionRunMode(row.run_mode),
        state_snapshot=InterpretationState.model_validate(row.state_snapshot),
        markdown=row.markdown,
        trigger_type=row.trigger_type,  # type: ignore[arg-type]
        input_version_id=row.input_version_id,
        override_snapshot=InterpretationOverride.model_validate(row.override_snapshot),
        start_step=StepId(row.start_step) if row.start_step is not None else None,
        source_execution_id=row.source_execution_id,
        planning_reason=row.planning_reason,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _confirm_waiting_stage_snapshot(
    row: ExecutionRow,
    stage: InterpretationStage,
    expected_stage_run_id: str,
    actor: str,
) -> InterpretationState:
    """在 Execution 行锁内核对并确认唯一 StageRun，避免并发重复确认。"""

    state = InterpretationState.model_validate(row.state_snapshot)
    matches = [
        (index, run)
        for index, run in enumerate(state.stage_runs)
        if run.task_id == row.task_id
        and run.execution_id == row.execution_id
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


def _as_input_version(row: InputVersionRow) -> InterpretationInputVersion:
    """反序列化时重新验证规范化输入及内容摘要。"""

    return InterpretationInputVersion(
        input_version_id=row.input_version_id,
        task_id=row.task_id,
        well_id=row.well_id,
        sequence=row.sequence,
        source_type=row.source_type,  # type: ignore[arg-type]
        payload_kind=InputPayloadKind(row.payload_kind),
        content_sha256=row.content_sha256,
        payload=MockFixture.model_validate(row.payload) if row.payload is not None else None,
        source_artifact_id=row.source_artifact_id,
        dataset_manifest=(
            GdsxDatasetManifest.model_validate(row.dataset_manifest)
            if row.dataset_manifest is not None
            else None
        ),
        created_at=row.created_at,
    )


def _as_artifact(row: ArtifactRow) -> Artifact:
    """读回 Artifact 元数据时重新校验逻辑 storage key 和归属字段。"""

    return Artifact(
        artifact_id=row.artifact_id,
        task_id=row.task_id,
        well_id=row.well_id,
        kind=ArtifactKind(row.kind),
        media_type=row.media_type,
        original_filename=row.original_filename,
        size_bytes=row.size_bytes,
        content_sha256=row.content_sha256,
        storage_key=row.storage_key,
        source_artifact_id=row.source_artifact_id,
        created_from_execution_id=row.created_from_execution_id,
        provider_call_id=row.provider_call_id,
        created_at=row.created_at,
    )


def _as_dataset_revision(row: DatasetRevisionRow) -> DatasetRevision:
    """读回 DatasetRevision metadata，不触发物化。"""

    return DatasetRevision(
        dataset_revision_id=row.dataset_revision_id,
        task_id=row.task_id,
        well_id=row.well_id,
        sequence=row.sequence,
        root_input_version_id=row.root_input_version_id,
        parent_revision_id=row.parent_revision_id,
        change_set_id=row.change_set_id,
        lineage_sha256=row.lineage_sha256,
        created_from_execution_id=row.created_from_execution_id,
        created_at=row.created_at,
    )


def _as_dataset_change_set(row: DatasetChangeSetRow) -> DatasetChangeSet:
    """读回并验证稀疏 ChangeSet。"""

    return DatasetChangeSet(
        change_set_id=row.change_set_id,
        task_id=row.task_id,
        well_id=row.well_id,
        base_revision_id=row.base_revision_id,
        change_type=DatasetChangeType(row.change_type),
        curve_changes=[DatasetCurveChange.model_validate(item) for item in row.payload],
        content_sha256=row.content_sha256,
        created_by=row.created_by,
        source_execution_id=row.source_execution_id,
        reason=row.reason,
        original_instruction=row.original_instruction,
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
                session.add(
                    ToolRunRow(
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
                    )
                )
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
        source_external_call_id: str | None = None,
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
                finished = ToolRun.model_validate(
                    {
                        **_as_tool_run(row).model_dump(mode="python"),
                        "status": status,
                        "source": source,
                        "output_snapshot": output_snapshot,
                        "finished_at": utc_now(),
                        "error_code": error_code,
                        "error_message": error_message,
                        "source_external_call_id": source_external_call_id,
                    }
                )
                row.status = finished.status.value
                row.source = finished.source
                row.output_snapshot = finished.output_snapshot
                row.finished_at = finished.finished_at
                row.error_code = finished.error_code
                row.error_message = finished.error_message
                row.source_external_call_id = finished.source_external_call_id
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
                rows = (
                    await session.scalars(
                        select(ToolRunRow)
                        .where(ToolRunRow.execution_id == execution_id)
                        .order_by(ToolRunRow.started_at, ToolRunRow.tool_run_id)
                    )
                ).all()
                return [_as_tool_run(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_TOOL_RUN", "数据库工具审计结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "DATABASE_READ_FAILED", "数据库工具审计列表读取失败"
            ) from None

    async def create_company_provider_call(self, call: CompanyProviderCall) -> CompanyProviderCall:
        """在单事务内核对 Execution 与 InputVersion 归属并创建运行态。"""

        try:
            call = CompanyProviderCall.model_validate(call.model_dump(mode="python"))
            async with self.sessions.begin() as session:
                execution = await session.get(ExecutionRow, call.execution_id)
                input_version = await session.get(InputVersionRow, call.input_version_id)
                if (
                    execution is None
                    or execution.task_id != call.task_id
                    or execution.input_version_id != call.input_version_id
                    or input_version is None
                    or input_version.task_id != call.task_id
                ):
                    raise InfrastructureError(
                        "COMPANY_RESULT_VERSION_MISMATCH",
                        "公司调用不属于当前执行及输入版本",
                    )
                session.add(
                    CompanyProviderCallRow(
                        provider_call_id=call.provider_call_id,
                        external_call_id=call.external_call_id,
                        task_id=call.task_id,
                        execution_id=call.execution_id,
                        input_version_id=call.input_version_id,
                        provider_operation=call.provider_operation.value,
                        status=call.status.value,
                        request_summary=call.request_summary,
                        normalized_result=call.normalized_result,
                        started_at=call.started_at,
                        finished_at=None,
                        error_code=None,
                    )
                )
            return call.model_copy(deep=True)
        except IntegrityError:
            raise InfrastructureError(
                "COMPANY_PROVIDER_CALL_EXISTS", "公司调用标识已存在"
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库公司调用创建失败") from None

    async def finish_company_provider_call(
        self,
        external_call_id: str,
        *,
        status: CompanyProviderCallStatus,
        normalized_result: JsonObject,
        error_code: str | None = None,
    ) -> CompanyProviderCall:
        """行锁保证调用只能结束一次；失败时 normalized_result 必须为空。"""

        try:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(CompanyProviderCallRow)
                    .where(CompanyProviderCallRow.external_call_id == external_call_id)
                    .with_for_update()
                )
                if row is None:
                    raise InfrastructureError("COMPANY_PROVIDER_CALL_NOT_FOUND", "公司调用不存在")
                current = _as_company_provider_call(row)
                if current.status != CompanyProviderCallStatus.RUNNING:
                    raise InfrastructureError("COMPANY_PROVIDER_CALL_FINISHED", "公司调用已经结束")
                finished = CompanyProviderCall.model_validate(
                    {
                        **current.model_dump(mode="python"),
                        "status": status,
                        "normalized_result": normalized_result,
                        "finished_at": utc_now(),
                        "error_code": error_code,
                    }
                )
                row.status = finished.status.value
                row.normalized_result = finished.normalized_result
                row.finished_at = finished.finished_at
                row.error_code = finished.error_code
            return finished
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库公司调用结束失败") from None

    async def get_company_provider_call(self, external_call_id: str) -> CompanyProviderCall | None:
        try:
            async with self.sessions() as session:
                row = await session.scalar(
                    select(CompanyProviderCallRow).where(
                        CompanyProviderCallRow.external_call_id == external_call_id
                    )
                )
                return _as_company_provider_call(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_COMPANY_PROVIDER_CALL", "公司调用持久事实无效"
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库公司调用读取失败") from None

    async def list_company_provider_calls(
        self,
        task_id: str,
        *,
        execution_id: str | None = None,
        operation: CompanyProviderOperation | None = None,
    ) -> list[CompanyProviderCall]:
        try:
            async with self.sessions() as session:
                statement = select(CompanyProviderCallRow).where(
                    CompanyProviderCallRow.task_id == task_id
                )
                if execution_id is not None:
                    statement = statement.where(CompanyProviderCallRow.execution_id == execution_id)
                if operation is not None:
                    statement = statement.where(
                        CompanyProviderCallRow.provider_operation == operation.value
                    )
                rows = (
                    await session.scalars(
                        statement.order_by(
                            CompanyProviderCallRow.started_at,
                            CompanyProviderCallRow.provider_call_id,
                        )
                    )
                ).all()
                return [_as_company_provider_call(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError(
                "INVALID_STORED_COMPANY_PROVIDER_CALL", "公司调用持久事实无效"
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "DATABASE_READ_FAILED", "数据库公司调用列表读取失败"
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

    async def create_gdsx_ingress(
        self,
        state: InterpretationState,
        artifact: Artifact,
        manifest: GdsxDatasetManifest,
    ) -> tuple[InterpretationInputVersion, DatasetRevision, Execution]:
        """一个数据库事务创建首个 GDSX Task、Artifact、Input、R1 与 Execution。"""

        task_id = state.task.task_id
        if artifact.task_id != task_id or artifact.well_id != state.task.well_id:
            raise InfrastructureError("ARTIFACT_TASK_MISMATCH", "源制品归属无效")
        version = InterpretationInputVersion(
            task_id=task_id,
            well_id=state.task.well_id,
            sequence=1,
            source_type="UPLOAD",
            payload_kind=InputPayloadKind.GDSX_ARTIFACT,
            content_sha256=artifact.content_sha256,
            source_artifact_id=artifact.artifact_id,
            dataset_manifest=manifest,
        )
        revision = DatasetRevision(
            task_id=task_id,
            well_id=state.task.well_id,
            sequence=1,
            root_input_version_id=version.input_version_id,
            lineage_sha256=root_lineage(version.content_sha256),
        )
        state.input_version_id = version.input_version_id
        state.source_artifact_id = artifact.artifact_id
        state.dataset_manifest = manifest
        state.dataset_revision_id = revision.dataset_revision_id
        now = utc_now()
        execution = Execution(
            execution_id=state.workflow_execution_id,
            task_id=task_id,
            sequence=1,
            status=ExecutionStatus.QUEUED,
            run_mode=ExecutionRunMode.STAGED_CONFIRMATION,
            state_snapshot=state.model_copy(deep=True),
            trigger_type="INITIAL",
            input_version_id=version.input_version_id,
            planning_reason="INITIAL",
            created_at=now,
            updated_at=now,
        )
        try:
            async with self.sessions.begin() as session:
                if await session.get(TaskRow, task_id) is not None:
                    raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务")
                task = TaskRow(
                    task_id=task_id,
                    well_id=state.task.well_id,
                    status=state.status.value,
                    snapshot=state.model_dump(mode="json"),
                    markdown="",
                    created_at=state.created_at,
                    updated_at=now,
                )
                session.add(task)
                await session.flush()
                session.add(
                    ArtifactRow(
                        **artifact.model_dump(mode="python", exclude={"kind"}),
                        kind=artifact.kind.value,
                    )
                )
                await session.flush()
                session.add(
                    InputVersionRow(
                        input_version_id=version.input_version_id,
                        task_id=task_id,
                        well_id=version.well_id,
                        sequence=1,
                        source_type="UPLOAD",
                        payload_kind=version.payload_kind.value,
                        content_sha256=version.content_sha256,
                        payload=None,
                        source_artifact_id=artifact.artifact_id,
                        dataset_manifest=manifest.model_dump(mode="json"),
                        created_at=version.created_at,
                    )
                )
                await session.flush()
                session.add(DatasetRevisionRow(**revision.model_dump(mode="python")))
                session.add(
                    ExecutionRow(
                        execution_id=execution.execution_id,
                        task_id=task_id,
                        sequence=1,
                        status=execution.status.value,
                        run_mode=execution.run_mode.value,
                        state_snapshot=state.model_dump(mode="json"),
                        markdown="",
                        trigger_type="INITIAL",
                        input_version_id=version.input_version_id,
                        override_snapshot=execution.override_snapshot.model_dump(mode="json"),
                        start_step=StepId.W01.value,
                        source_execution_id=None,
                        planning_reason="INITIAL",
                        created_at=now,
                        updated_at=now,
                    )
                )
                task.current_execution_id = execution.execution_id
                task.current_input_version_id = version.input_version_id
            return version, revision, execution
        except IntegrityError:
            raise InfrastructureError("GDSX_INGRESS_CONFLICT", "GDSX 首次导入事实冲突") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库 GDSX 导入失败") from None

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

    async def bind_task_to_session(self, binding: SessionTaskBinding) -> None:
        """用 PostgreSQL 冲突忽略保证同一四元组可安全重复绑定。"""

        binding = SessionTaskBinding.model_validate(binding.model_dump(mode="python"))
        try:
            async with self.sessions.begin() as session:
                task = await session.get(TaskRow, binding.task_id)
                if task is None:
                    raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
                statement = (
                    postgresql_insert(SessionTaskBindingRow)
                    .values(
                        user_id=binding.user_id,
                        agent_id=binding.agent_id,
                        session_id=binding.session_id,
                        task_id=binding.task_id,
                        created_at=binding.created_at,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["user_id", "agent_id", "session_id", "task_id"]
                    )
                )
                await session.execute(statement)
        except InfrastructureError:
            raise
        except (IntegrityError, SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "SESSION_TASK_BINDING_FAILED", "会话任务归属保存失败"
            ) from None

    async def list_session_task_ids(self, identity: TaskSessionIdentity) -> list[str]:
        """从持久绑定恢复一个完整会话身份下的所有任务标识。"""

        identity = TaskSessionIdentity.model_validate(identity.model_dump(mode="python"))
        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(SessionTaskBindingRow.task_id)
                        .where(
                            SessionTaskBindingRow.user_id == identity.user_id,
                            SessionTaskBindingRow.agent_id == identity.agent_id,
                            SessionTaskBindingRow.session_id == identity.session_id,
                        )
                        .order_by(
                            SessionTaskBindingRow.created_at,
                            SessionTaskBindingRow.task_id,
                        )
                    )
                ).all()
                return list(rows)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "SESSION_TASK_BINDING_READ_FAILED", "会话任务归属读取失败"
            ) from None

    async def task_belongs_to_session(self, identity: TaskSessionIdentity, task_id: str) -> bool:
        """查询完整四元组；单独存在的 task_id 不构成访问授权。"""

        identity = TaskSessionIdentity.model_validate(identity.model_dump(mode="python"))
        try:
            async with self.sessions() as session:
                result = await session.scalar(
                    select(SessionTaskBindingRow.task_id).where(
                        SessionTaskBindingRow.user_id == identity.user_id,
                        SessionTaskBindingRow.agent_id == identity.agent_id,
                        SessionTaskBindingRow.session_id == identity.session_id,
                        SessionTaskBindingRow.task_id == task_id,
                    )
                )
                return result is not None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "SESSION_TASK_BINDING_READ_FAILED", "会话任务归属读取失败"
            ) from None

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
                    payload_kind=version.payload_kind.value,
                    content_sha256=version.content_sha256,
                    payload=fixture.model_dump(mode="json"),
                    source_artifact_id=None,
                    dataset_manifest=None,
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

    async def create_artifact(self, artifact: Artifact) -> Artifact:
        """只保存经过归属检查的 Artifact metadata。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.get(TaskRow, artifact.task_id)
                if task is None or task.well_id != artifact.well_id:
                    raise InfrastructureError("ARTIFACT_TASK_MISMATCH", "制品不属于当前任务或井")
                if artifact.source_artifact_id is not None:
                    source = await session.get(ArtifactRow, artifact.source_artifact_id)
                    if source is None or source.task_id != artifact.task_id:
                        raise InfrastructureError("ARTIFACT_TASK_MISMATCH", "源制品不属于当前任务")
                if artifact.created_from_execution_id is not None:
                    execution = await session.get(ExecutionRow, artifact.created_from_execution_id)
                    if execution is None or execution.task_id != artifact.task_id:
                        raise InfrastructureError(
                            "ARTIFACT_TASK_MISMATCH", "来源执行不属于当前任务"
                        )
                if artifact.provider_call_id is not None:
                    provider_call = await session.get(
                        CompanyProviderCallRow, artifact.provider_call_id
                    )
                    if provider_call is None or provider_call.task_id != artifact.task_id:
                        raise InfrastructureError(
                            "ARTIFACT_TASK_MISMATCH", "ProviderCall 不属于任务"
                        )
                session.add(
                    ArtifactRow(
                        **artifact.model_dump(mode="python", exclude={"kind"}),
                        kind=artifact.kind.value,
                    )
                )
            return artifact.model_copy(deep=True)
        except IntegrityError:
            raise InfrastructureError("ARTIFACT_CONFLICT", "制品元数据已存在或引用无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库制品元数据写入失败") from None

    async def get_artifact(self, task_id: str, artifact_id: str) -> Artifact | None:
        """task_id 与 artifact_id 必须同时匹配，禁止跨任务读取。"""

        try:
            async with self.sessions() as session:
                row = await session.scalar(
                    select(ArtifactRow).where(
                        ArtifactRow.task_id == task_id, ArtifactRow.artifact_id == artifact_id
                    )
                )
                return _as_artifact(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_ARTIFACT", "数据库制品元数据无效") from None

    async def list_artifacts(self, task_id: str) -> list[Artifact]:
        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(ArtifactRow)
                        .where(ArtifactRow.task_id == task_id)
                        .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
                    )
                ).all()
                return [_as_artifact(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_ARTIFACT", "数据库制品元数据无效") from None

    async def create_artifact_input_version(
        self,
        task_id: str,
        *,
        well_id: str,
        artifact: Artifact,
        manifest: GdsxDatasetManifest,
        source_type: InputSource = "UPLOAD",
    ) -> InterpretationInputVersion:
        """任务行锁内创建只引用 Artifact 的 InputVersion，不复制 bytes。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == task_id).with_for_update()
                )
                artifact_row = await session.get(ArtifactRow, artifact.artifact_id)
                if (
                    task is None
                    or task.well_id != well_id
                    or artifact_row is None
                    or artifact_row.task_id != task_id
                    or artifact_row.well_id != well_id
                ):
                    raise InfrastructureError("ARTIFACT_TASK_MISMATCH", "制品输入归属无效")
                last_sequence = await session.scalar(
                    select(func.max(InputVersionRow.sequence)).where(
                        InputVersionRow.task_id == task_id
                    )
                )
                version = InterpretationInputVersion(
                    task_id=task_id,
                    well_id=well_id,
                    sequence=(last_sequence or 0) + 1,
                    source_type=source_type,
                    payload_kind=InputPayloadKind.GDSX_ARTIFACT,
                    content_sha256=artifact.content_sha256,
                    source_artifact_id=artifact.artifact_id,
                    dataset_manifest=manifest,
                )
                session.add(
                    InputVersionRow(
                        input_version_id=version.input_version_id,
                        task_id=task_id,
                        well_id=well_id,
                        sequence=version.sequence,
                        source_type=source_type,
                        payload_kind=version.payload_kind.value,
                        content_sha256=version.content_sha256,
                        payload=None,
                        source_artifact_id=artifact.artifact_id,
                        dataset_manifest=manifest.model_dump(mode="json"),
                        created_at=version.created_at,
                    )
                )
                task.current_input_version_id = version.input_version_id
                task.updated_at = utc_now()
            return version
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
                rows = (
                    await session.scalars(
                        select(InputVersionRow)
                        .where(InputVersionRow.task_id == task_id)
                        .order_by(InputVersionRow.sequence)
                    )
                ).all()
                return [_as_input_version(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_INPUT", "数据库输入版本结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "DATABASE_READ_FAILED", "数据库输入版本列表读取失败"
            ) from None

    async def create_root_dataset_revision(self, revision: DatasetRevision) -> DatasetRevision:
        """任务锁内创建 metadata-only 根版本。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == revision.task_id).with_for_update()
                )
                input_row = await session.get(InputVersionRow, revision.root_input_version_id)
                existing = await session.scalar(
                    select(DatasetRevisionRow.dataset_revision_id).where(
                        DatasetRevisionRow.task_id == revision.task_id
                    )
                )
                if task is None or input_row is None:
                    raise InfrastructureError("DATASET_REVISION_TASK_MISMATCH", "根版本归属无效")
                if (
                    input_row.task_id != revision.task_id
                    or input_row.well_id != revision.well_id
                    or task.well_id != revision.well_id
                ):
                    raise InfrastructureError(
                        "DATASET_REVISION_TASK_MISMATCH", "根输入不属于任务或井"
                    )
                if existing is not None:
                    raise InfrastructureError("DATASET_ROOT_EXISTS", "任务已存在根数据集版本")
                if revision.parent_revision_id is not None or revision.change_set_id is not None:
                    raise InfrastructureError("INVALID_DATASET_REVISION_CHAIN", "根版本引用无效")
                if revision.sequence != 1 or revision.lineage_sha256 != root_lineage(
                    input_row.content_sha256
                ):
                    raise InfrastructureError(
                        "INVALID_DATASET_REVISION_CHAIN", "根版本摘要或序号无效"
                    )
                if revision.created_from_execution_id is not None:
                    execution = await session.get(ExecutionRow, revision.created_from_execution_id)
                    if execution is None or execution.task_id != revision.task_id:
                        raise InfrastructureError(
                            "EXECUTION_TASK_MISMATCH", "来源执行不属于当前任务"
                        )
                session.add(
                    DatasetRevisionRow(
                        dataset_revision_id=revision.dataset_revision_id,
                        task_id=revision.task_id,
                        well_id=revision.well_id,
                        sequence=1,
                        root_input_version_id=revision.root_input_version_id,
                        lineage_sha256=revision.lineage_sha256,
                        created_from_execution_id=revision.created_from_execution_id,
                        created_at=revision.created_at,
                    )
                )
            return revision.model_copy(update={"sequence": 1}, deep=True)
        except IntegrityError:
            raise InfrastructureError("DATASET_ROOT_EXISTS", "根数据集版本已存在") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库根版本创建失败") from None

    async def create_revision_with_change_set(
        self, revision: DatasetRevision, change_set: DatasetChangeSet
    ) -> tuple[DatasetRevision, DatasetChangeSet]:
        """一个事务创建稀疏 ChangeSet 和 Child Revision，禁止 orphan。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == revision.task_id).with_for_update()
                )
                parent = await session.get(DatasetRevisionRow, revision.parent_revision_id)
                if task is None or parent is None or parent.task_id != revision.task_id:
                    raise InfrastructureError("DATASET_REVISION_TASK_MISMATCH", "父版本归属无效")
                if (
                    task.well_id != revision.well_id
                    or parent.well_id != revision.well_id
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
                for execution_id in (
                    revision.created_from_execution_id,
                    change_set.source_execution_id,
                ):
                    if execution_id is not None:
                        execution = await session.get(ExecutionRow, execution_id)
                        if execution is None or execution.task_id != revision.task_id:
                            raise InfrastructureError(
                                "EXECUTION_TASK_MISMATCH", "来源执行不属于当前任务"
                            )
                last_sequence = await session.scalar(
                    select(func.max(DatasetRevisionRow.sequence)).where(
                        DatasetRevisionRow.task_id == revision.task_id
                    )
                )
                revision = DatasetRevision.model_validate(
                    {
                        **revision.model_dump(mode="python"),
                        "sequence": (last_sequence or 0) + 1,
                    }
                )
                session.add(
                    DatasetChangeSetRow(
                        change_set_id=change_set.change_set_id,
                        task_id=change_set.task_id,
                        well_id=change_set.well_id,
                        base_revision_id=change_set.base_revision_id,
                        change_type=change_set.change_type.value,
                        payload=[item.model_dump(mode="json") for item in change_set.curve_changes],
                        content_sha256=change_set.content_sha256,
                        created_by=change_set.created_by,
                        source_execution_id=change_set.source_execution_id,
                        reason=change_set.reason,
                        original_instruction=change_set.original_instruction,
                        created_at=change_set.created_at,
                    )
                )
                await session.flush()
                session.add(
                    DatasetRevisionRow(
                        dataset_revision_id=revision.dataset_revision_id,
                        task_id=revision.task_id,
                        well_id=revision.well_id,
                        sequence=revision.sequence,
                        root_input_version_id=revision.root_input_version_id,
                        parent_revision_id=revision.parent_revision_id,
                        change_set_id=revision.change_set_id,
                        lineage_sha256=revision.lineage_sha256,
                        created_from_execution_id=revision.created_from_execution_id,
                        created_at=revision.created_at,
                    )
                )
            return revision.model_copy(deep=True), change_set.model_copy(deep=True)
        except IntegrityError:
            raise InfrastructureError(
                "DATASET_REVISION_CONFLICT", "数据集版本或 ChangeSet 冲突"
            ) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库数据集版本创建失败") from None

    async def get_dataset_revision(self, revision_id: str) -> DatasetRevision | None:
        try:
            async with self.sessions() as session:
                row = await session.get(DatasetRevisionRow, revision_id)
                return _as_dataset_revision(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_DATASET_REVISION", "数据集版本无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库版本读取失败") from None

    async def list_dataset_revisions(self, task_id: str) -> list[DatasetRevision]:
        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(DatasetRevisionRow)
                        .where(DatasetRevisionRow.task_id == task_id)
                        .order_by(DatasetRevisionRow.sequence)
                    )
                ).all()
                return [_as_dataset_revision(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_DATASET_REVISION", "数据集版本无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库版本列表读取失败") from None

    async def get_dataset_change_set(self, change_set_id: str) -> DatasetChangeSet | None:
        try:
            async with self.sessions() as session:
                row = await session.get(DatasetChangeSetRow, change_set_id)
                return _as_dataset_change_set(row) if row is not None else None
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_CHANGE_SET", "ChangeSet 结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库 ChangeSet 读取失败") from None

    async def list_dataset_change_sets(self, task_id: str) -> list[DatasetChangeSet]:
        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(DatasetChangeSetRow)
                        .where(DatasetChangeSetRow.task_id == task_id)
                        .order_by(DatasetChangeSetRow.created_at, DatasetChangeSetRow.change_set_id)
                    )
                ).all()
                return [_as_dataset_change_set(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_CHANGE_SET", "ChangeSet 结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError(
                "DATABASE_READ_FAILED", "数据库 ChangeSet 列表读取失败"
            ) from None

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
                if (
                    expected_current_execution_id is not None
                    and task.current_execution_id != expected_current_execution_id
                ):
                    raise InfrastructureError(
                        "STALE_EXECUTION_PLAN", "任务当前版本已变化，请重新规划"
                    )
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
                current = (
                    await session.get(ExecutionRow, task.current_execution_id)
                    if task.current_execution_id is not None
                    else None
                )
                if current is not None and current.status in {
                    ExecutionStatus.QUEUED.value,
                    ExecutionStatus.RUNNING.value,
                }:
                    raise InfrastructureError("TASK_EXECUTION_ACTIVE", "当前任务已有执行正在处理")
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
                    status=ExecutionStatus.QUEUED.value,
                    run_mode=run_mode.value,
                    state_snapshot=state.model_dump(mode="json"),
                    markdown="",
                    trigger_type=trigger_type,
                    input_version_id=input_version_id,
                    override_snapshot=(override_snapshot or InterpretationOverride()).model_dump(
                        mode="json"
                    ),
                    start_step=start_step.value if start_step is not None else None,
                    source_execution_id=source_execution_id,
                    planning_reason=planning_reason,
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
                rows = (
                    await session.scalars(
                        select(ExecutionRow)
                        .where(ExecutionRow.task_id == task_id)
                        .order_by(ExecutionRow.sequence)
                    )
                ).all()
                return [_as_execution(row) for row in rows]
        except (ValidationError, ValueError):
            raise InfrastructureError("INVALID_STORED_EXECUTION", "数据库执行结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库执行列表读取失败") from None

    async def claim_execution(
        self, execution_id: str, worker_id: str, lease_expires_at: datetime
    ) -> bool:
        """行锁保证多个 Worker 只有一个能把 QUEUED 转为 RUNNING。"""

        try:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(ExecutionRow)
                    .where(ExecutionRow.execution_id == execution_id)
                    .with_for_update()
                )
                if row is None:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
                if row.status != ExecutionStatus.QUEUED.value:
                    return False
                now = utc_now()
                row.status = ExecutionStatus.RUNNING.value
                row.started_at = row.started_at or now
                row.lease_owner = worker_id
                row.lease_expires_at = lease_expires_at
                row.updated_at = now
                task = await session.get(TaskRow, row.task_id)
                if task is not None and task.current_execution_id == execution_id:
                    task.status = ExecutionStatus.RUNNING.value
                    task.updated_at = now
                return True
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库执行认领失败") from None

    async def renew_execution_lease(
        self, execution_id: str, worker_id: str, lease_expires_at: datetime
    ) -> bool:
        """续约同时核对 owner、运行态和原 lease 尚未过期。"""

        try:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(ExecutionRow)
                    .where(ExecutionRow.execution_id == execution_id)
                    .with_for_update()
                )
                now = utc_now()
                if (
                    row is None
                    or row.status != ExecutionStatus.RUNNING.value
                    or row.lease_owner != worker_id
                    or row.lease_expires_at is None
                    or row.lease_expires_at <= now
                ):
                    return False
                row.lease_expires_at = lease_expires_at
                row.updated_at = now
                return True
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库 lease 续约失败") from None

    async def finish_execution(
        self,
        execution_id: str,
        worker_id: str,
        status: ExecutionStatus,
        error_code: str | None = None,
    ) -> Execution:
        """终态、成功指针和 lease 清理在同一事务内提交。"""

        if status not in TERMINAL_EXECUTION_STATUSES:
            raise InfrastructureError("INVALID_EXECUTION_STATUS", "执行只能写入终态")
        try:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(ExecutionRow)
                    .where(ExecutionRow.execution_id == execution_id)
                    .with_for_update()
                )
                if row is None:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
                if row.status != ExecutionStatus.RUNNING.value:
                    raise InfrastructureError("EXECUTION_NOT_RUNNING", "执行不在运行态")
                if row.lease_owner != worker_id:
                    raise InfrastructureError(
                        "EXECUTION_LEASE_MISMATCH", "执行 lease 不属于当前 Worker"
                    )
                if status in {ExecutionStatus.SUCCESS, ExecutionStatus.WARNING} and (
                    row.lease_expires_at is None or row.lease_expires_at <= utc_now()
                ):
                    raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == row.task_id).with_for_update()
                )
                assert task is not None
                now = utc_now()
                row.status = status.value
                row.finished_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.error_code = error_code
                row.updated_at = now
                task.status = status.value
                task.updated_at = now
                if status in {ExecutionStatus.SUCCESS, ExecutionStatus.WARNING} and row.markdown:
                    task.latest_successful_execution_id = execution_id
                result = _as_execution(row)
            return result
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库执行终结失败") from None

    async def pause_execution_for_confirmation(
        self,
        execution_id: str,
        worker_id: str,
        state: InterpretationState,
        markdown: str = "",
    ) -> Execution:
        """保存阶段候选、清理 lease 和更新 Task 兼容视图，全部在一个事务内。"""

        try:
            async with self.sessions.begin() as session:
                identity = await session.get(ExecutionRow, execution_id)
                if identity is None:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == identity.task_id).with_for_update()
                )
                row = await session.scalar(
                    select(ExecutionRow)
                    .where(ExecutionRow.execution_id == execution_id)
                    .with_for_update()
                )
                if row is None or task is None:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
                if task.current_execution_id != execution_id:
                    raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不能进入确认态")
                if row.run_mode != ExecutionRunMode.STAGED_CONFIRMATION.value:
                    raise InfrastructureError("INVALID_EXECUTION_MODE", "执行不是分阶段确认模式")
                if row.status != ExecutionStatus.RUNNING.value or row.lease_owner != worker_id:
                    raise InfrastructureError(
                        "EXECUTION_LEASE_MISMATCH", "执行不由当前 Worker 持有"
                    )
                if row.lease_expires_at is None or row.lease_expires_at <= utc_now():
                    raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
                waiting = [
                    run
                    for run in state.stage_runs
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
                payload = state.model_dump(mode="json")
                row.status = ExecutionStatus.WAITING_CONFIRMATION.value
                row.state_snapshot = payload
                row.markdown = markdown
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                task.status = row.status
                task.snapshot = payload
                task.markdown = markdown
                task.updated_at = now
                result = _as_execution(row)
            return result
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库阶段暂停失败") from None

    async def confirm_stage_and_requeue(
        self,
        task_id: str,
        execution_id: str,
        stage: InterpretationStage,
        expected_stage_run_id: str,
        actor: str,
    ) -> Execution:
        """确认非报告阶段并原子重排同一 Execution，不创建新版本。"""

        if stage == InterpretationStage.REPORT:
            raise InfrastructureError("INVALID_STAGE", "报告阶段应使用最终确认入口")
        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == task_id).with_for_update()
                )
                row = await session.scalar(
                    select(ExecutionRow)
                    .where(ExecutionRow.execution_id == execution_id)
                    .with_for_update()
                )
                if task is None or row is None or row.task_id != task_id:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
                if task.current_execution_id != execution_id:
                    raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不能确认")
                if row.status != ExecutionStatus.WAITING_CONFIRMATION.value:
                    raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "执行不在等待确认态")
                state = _confirm_waiting_stage_snapshot(row, stage, expected_stage_run_id, actor)
                now = utc_now()
                payload = state.model_dump(mode="json")
                row.status = ExecutionStatus.QUEUED.value
                row.state_snapshot = payload
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                task.status = row.status
                task.snapshot = payload
                task.updated_at = now
                result = _as_execution(row)
            return result
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库阶段确认失败") from None

    async def confirm_final_stage_and_finish(
        self,
        task_id: str,
        execution_id: str,
        expected_stage_run_id: str,
        actor: str,
    ) -> Execution:
        """确认报告并原子写入最终状态、完成时间和最新成功指针。"""

        try:
            async with self.sessions.begin() as session:
                task = await session.scalar(
                    select(TaskRow).where(TaskRow.task_id == task_id).with_for_update()
                )
                row = await session.scalar(
                    select(ExecutionRow)
                    .where(ExecutionRow.execution_id == execution_id)
                    .with_for_update()
                )
                if task is None or row is None or row.task_id != task_id:
                    raise InfrastructureError("EXECUTION_NOT_FOUND", "执行不存在")
                if task.current_execution_id != execution_id:
                    raise InfrastructureError("EXECUTION_NOT_CURRENT", "历史执行不能确认")
                if row.status != ExecutionStatus.WAITING_CONFIRMATION.value:
                    raise InfrastructureError("STAGE_CONFIRMATION_CONFLICT", "执行不在等待确认态")
                if not row.markdown:
                    raise InfrastructureError("REPORT_NOT_READY", "报告候选尚未生成")
                state = _confirm_waiting_stage_snapshot(
                    row, InterpretationStage.REPORT, expected_stage_run_id, actor
                )
                status = execution_status_from_state(state.completed_status())
                now = utc_now()
                payload = state.model_dump(mode="json")
                row.status = status.value
                row.state_snapshot = payload
                row.finished_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                task.status = row.status
                task.snapshot = payload
                task.markdown = row.markdown
                task.latest_successful_execution_id = execution_id
                task.updated_at = now
                result = _as_execution(row)
            return result
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库最终确认失败") from None

    async def recover_expired_executions(self, now: datetime) -> list[str]:
        """锁定所有过期 RUNNING 并失败，不自动再次调用 Workflow。"""

        try:
            async with self.sessions.begin() as session:
                rows = (
                    await session.scalars(
                        select(ExecutionRow)
                        .where(
                            ExecutionRow.status == ExecutionStatus.RUNNING.value,
                            ExecutionRow.lease_expires_at.is_not(None),
                            ExecutionRow.lease_expires_at <= now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for row in rows:
                    row.status = ExecutionStatus.FAILED.value
                    row.finished_at = now
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.error_code = "WORKER_LEASE_EXPIRED"
                    row.updated_at = now
                    task = await session.get(TaskRow, row.task_id)
                    if task is not None and task.current_execution_id == row.execution_id:
                        task.status = ExecutionStatus.FAILED.value
                        task.updated_at = now
                return [row.execution_id for row in rows]
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库过期执行恢复失败") from None

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
                if row.status != ExecutionStatus.RUNNING.value:
                    raise InfrastructureError("EXECUTION_NOT_RUNNING", "执行不在运行态")
                if row.lease_expires_at is None or row.lease_expires_at <= utc_now():
                    raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
                payload = state.model_dump(mode="json")
                now = utc_now()
                row.state_snapshot = payload
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
                        status=ExecutionStatus.QUEUED.value,
                        run_mode=ExecutionRunMode.CONTINUOUS.value,
                        state_snapshot=payload,
                        markdown="",
                        trigger_type="INITIAL",
                        input_version_id=None,
                        override_snapshot={},
                        start_step=StepId.W01.value,
                        planning_reason="INITIAL",
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
                if row.error_code == "WORKER_LEASE_EXPIRED":
                    raise InfrastructureError("EXECUTION_NOT_RUNNING", "过期执行不可再写入")
                if row.status == ExecutionStatus.RUNNING.value and (
                    row.lease_expires_at is None or row.lease_expires_at <= utc_now()
                ):
                    raise InfrastructureError("EXECUTION_LEASE_EXPIRED", "执行租约已过期")
                now = utc_now()
                payload = state.model_dump(mode="json")
                row.state_snapshot = payload
                row.markdown = markdown
                row.updated_at = now
                task.snapshot = payload
                task.status = row.status
                task.markdown = markdown
                task.updated_at = now
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
