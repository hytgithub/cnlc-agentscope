"""四阶段业务边界、运行历史和依赖规则；不执行算法或访问存储。"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import uuid4

from pydantic import Field, model_validator

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.errors import WorkflowError
from cnlc_agent.domain.models import Contract, ErrorDetail, utc_now


class InterpretationStage(StrEnum):
    """复用既有规划阶段值，DECODE 别名保持 Demo 的 DATA_DECODE 协议兼容。"""

    DATA_DECODE = "DATA_DECODE"
    DECODE = "DATA_DECODE"
    PREPROCESS = "PREPROCESS"
    INTERPRET = "INTERPRET"
    REPORT = "REPORT"


ExecutionStage = InterpretationStage
STAGE_ORDER = tuple(InterpretationStage)
STAGE_STEPS = {
    InterpretationStage.DECODE: (StepId.W01,),
    InterpretationStage.PREPROCESS: (StepId.W02, StepId.W03),
    InterpretationStage.INTERPRET: (
        StepId.W04,
        StepId.W05,
        StepId.W06,
        StepId.W07,
        StepId.W08,
        StepId.W09,
        StepId.W10,
    ),
    InterpretationStage.REPORT: (),
}


class StageRunStatus(StrEnum):
    """阶段一次实际执行的生命周期，不承载后续修改造成的有效性变化。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRM = "WAITING_CONFIRM"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class StageValidity(StrEnum):
    """当前工作版本对既有阶段结果的有效性判断。"""

    CURRENT = "CURRENT"
    STALE = "STALE"


class StageImpact(StrEnum):
    """修改的业务影响类型，不代替现有 InterpretationOverride 参数模型。"""

    DATASET_PATCH = "DatasetPatch"
    PREPROCESS_PARAMETER_CHANGE = "PreprocessParameterChange"
    INTERPRETATION_PARAMETER_CHANGE = "InterpretationParameterChange"
    INTERPRETATION_OVERRIDE = "InterpretationOverride"
    REPORT_CONFIG_CHANGE = "ReportConfigChange"


IMPACT_START = {
    StageImpact.DATASET_PATCH: InterpretationStage.PREPROCESS,
    StageImpact.PREPROCESS_PARAMETER_CHANGE: InterpretationStage.PREPROCESS,
    StageImpact.INTERPRETATION_PARAMETER_CHANGE: InterpretationStage.INTERPRET,
    StageImpact.INTERPRETATION_OVERRIDE: InterpretationStage.INTERPRET,
    StageImpact.REPORT_CONFIG_CHANGE: InterpretationStage.REPORT,
}
OUTPUT_KEY = {
    InterpretationStage.DECODE: "dataset_revision_id",
    InterpretationStage.PREPROCESS: "preprocess_revision_id",
    InterpretationStage.INTERPRET: "interpretation_revision_id",
    InterpretationStage.REPORT: "report_revision_id",
}
DEPENDENCIES = {
    InterpretationStage.DECODE: (),
    InterpretationStage.PREPROCESS: (InterpretationStage.DECODE,),
    InterpretationStage.INTERPRET: (InterpretationStage.DECODE, InterpretationStage.PREPROCESS),
    InterpretationStage.REPORT: (InterpretationStage.INTERPRET,),
}
RefId = Annotated[str, Field(min_length=1, max_length=512)]


class StageTransition(Contract):
    """阶段执行状态审计随 Execution 快照持久化。"""

    before: StageRunStatus
    after: StageRunStatus
    actor: RefId
    reason: RefId
    timestamp: datetime = Field(default_factory=utc_now)


class StageRun(Contract):
    """某任务某阶段的一次真实执行，refs 仅存逻辑 ID，不包含结果正文。"""

    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: RefId
    execution_id: RefId
    stage: InterpretationStage
    status: StageRunStatus = StageRunStatus.PENDING
    validity: StageValidity = StageValidity.CURRENT
    input_refs: dict[RefId, RefId] = Field(default_factory=dict)
    output_refs: dict[RefId, RefId] = Field(default_factory=dict)
    applied_change_ids: list[RefId] = Field(default_factory=list)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    confirmed_at: datetime | None = None
    confirmed_by: RefId | None = None
    stale_at: datetime | None = None
    stale_reason: RefId | None = None
    invalidated_by_change_id: RefId | None = None
    transitions: list[StageTransition] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        """读入快照也校验时间和正式结果，避免成功状态缺少必要信息。"""

        if self.status != StageRunStatus.PENDING and self.started_at is None:
            raise ValueError("started stage requires started_at")
        if (
            self.status
            in {
                StageRunStatus.WAITING_CONFIRM,
                StageRunStatus.CONFIRMED,
                StageRunStatus.FAILED,
            }
            and self.finished_at is None
        ):
            raise ValueError("finished stage requires finished_at")
        if self.status in {StageRunStatus.WAITING_CONFIRM, StageRunStatus.CONFIRMED}:
            if OUTPUT_KEY[self.stage] not in self.output_refs:
                raise ValueError("successful stage requires its output reference")
        if self.status == StageRunStatus.CONFIRMED and (
            self.confirmed_at is None or self.confirmed_by is None
        ):
            raise ValueError("confirmed stage requires actor and time")
        if self.status == StageRunStatus.FAILED and not self.errors:
            raise ValueError("failed stage requires errors")
        if (self.confirmed_at is None) != (self.confirmed_by is None):
            raise ValueError("confirmation actor and time must be recorded together")
        if (
            self.status in {StageRunStatus.PENDING, StageRunStatus.RUNNING}
            and self.finished_at is not None
        ):
            raise ValueError("unfinished stage cannot have finished_at")
        if self.status == StageRunStatus.PENDING and self.started_at is not None:
            raise ValueError("pending stage cannot have started_at")
        if self.status != StageRunStatus.CONFIRMED and self.confirmed_at:
            raise ValueError("unconfirmed stage cannot have confirmation metadata")
        stale_fields = (self.stale_at, self.stale_reason)
        if self.validity == StageValidity.STALE and any(item is None for item in stale_fields):
            raise ValueError("stale stage requires stale_at and stale_reason")
        if self.validity == StageValidity.CURRENT and any(
            item is not None for item in (*stale_fields, self.invalidated_by_change_id)
        ):
            raise ValueError("current stage cannot have stale metadata")
        if self.validity == StageValidity.STALE and self.status not in {
            StageRunStatus.WAITING_CONFIRM,
            StageRunStatus.CONFIRMED,
        }:
            raise ValueError("only a completed stage result can become stale")
        if len(self.applied_change_ids) != len(set(self.applied_change_ids)):
            raise ValueError("applied change ids must be unique")
        if any(
            value.startswith(("/", "~/", "file://"))
            or (len(value) >= 3 and value[1:3] in {":/", ":\\"})
            for value in (*self.input_refs.values(), *self.output_refs.values())
        ):
            raise ValueError("stage refs must be logical ids, not local paths")
        times = [
            item
            for item in (
                self.started_at,
                self.finished_at,
                self.confirmed_at,
                self.stale_at,
            )
            if item is not None
        ]
        if any(t.tzinfo is None or t.utcoffset() is None for t in times):
            raise ValueError("stage times require timezones")
        if self.started_at is not None and self.finished_at is not None:
            if self.started_at > self.finished_at:
                raise ValueError("stage times must be chronological")
        if self.finished_at is not None and self.stale_at is not None:
            if self.finished_at > self.stale_at:
                raise ValueError("stale time cannot precede stage completion")
        if self.finished_at is not None and self.confirmed_at is not None:
            if self.finished_at > self.confirmed_at:
                raise ValueError("confirmation cannot precede stage completion")
        return self

    def transition(
        self,
        target: StageRunStatus,
        *,
        actor: str,
        reason: str,
        output_refs: dict[str, str] | None = None,
        summary: str | None = None,
        warnings: list[str] | None = None,
        errors: list[ErrorDetail] | None = None,
    ) -> Self:
        """返回校验后的新执行快照；终态不可复活，重跑须创建新的运行 ID。"""

        allowed = {
            StageRunStatus.PENDING: {StageRunStatus.RUNNING},
            StageRunStatus.RUNNING: {
                StageRunStatus.WAITING_CONFIRM,
                StageRunStatus.CONFIRMED,
                StageRunStatus.FAILED,
            },
            StageRunStatus.WAITING_CONFIRM: {StageRunStatus.CONFIRMED},
        }
        if target not in allowed.get(self.status, set()):
            raise WorkflowError("INVALID_STAGE_TRANSITION", "非法阶段状态转换")
        now = utc_now()
        data = self.model_dump(mode="python")
        data["status"] = target
        if target == StageRunStatus.RUNNING:
            data["started_at"] = now
        elif self.status == StageRunStatus.RUNNING:
            data["finished_at"] = now
        if target == StageRunStatus.CONFIRMED:
            data.update(confirmed_at=now, confirmed_by=actor)
        # 确认和失效只能变更可用性，不能偷偷替换成功结果或诊断。
        if any(x is not None for x in (output_refs, summary, warnings, errors)):
            if self.status != StageRunStatus.RUNNING:
                raise WorkflowError("INVALID_STAGE_TRANSITION", "仅执行完成时可记录结果")
            for key, value in (
                ("output_refs", output_refs),
                ("summary", summary),
                ("warnings", warnings),
                ("errors", errors),
            ):
                if value is not None:
                    data[key] = value
        data["transitions"].append(
            StageTransition(
                before=self.status, after=target, actor=actor, reason=reason, timestamp=now
            )
        )
        return type(self).model_validate(data)


def confirm_stage(run: StageRun, *, actor: str = "user") -> StageRun:
    """人工确认的统一领域入口；自动确认在执行完成时记录 system。"""

    if run.status != StageRunStatus.WAITING_CONFIRM or run.validity != StageValidity.CURRENT:
        raise WorkflowError("INVALID_STAGE_TRANSITION", "只能确认等待确认的阶段")
    return run.transition(StageRunStatus.CONFIRMED, actor=actor, reason="确认阶段结果")


def latest_runs(runs: list[StageRun], task_id: str) -> dict[InterpretationStage, StageRun]:
    """按追加顺序选择最新运行，禁止回退到被最新失败或失效结果遮蔽的旧成功。"""

    return {run.stage: run for run in runs if run.task_id == task_id}


def invalidate_from(
    runs: list[StageRun],
    *,
    task_id: str,
    impact: StageImpact,
    actor: str,
    reason: str,
    change_id: str | None = None,
) -> list[StageRun]:
    """在副本中标记当前受影响的成功结果，保留旧 ID、引用、诊断与确认记录。"""

    return mark_downstream_stale(
        runs,
        task_id=task_id,
        start_stage=IMPACT_START[impact],
        actor=actor,
        reason=reason,
        change_id=change_id,
    )


def mark_downstream_stale(
    runs: list[StageRun],
    *,
    task_id: str,
    start_stage: InterpretationStage,
    actor: str,
    reason: str,
    change_id: str | None = None,
) -> list[StageRun]:
    """计划执行边界与修改影响共用同一失效传播规则；完整重跑可从解编开始。"""

    current = latest_runs(runs, task_id)
    affected = STAGE_ORDER[STAGE_ORDER.index(start_stage) :]
    if any(current[s].status == StageRunStatus.RUNNING for s in affected if s in current):
        raise WorkflowError("STAGE_CHANGE_DURING_RUN", "运行中不能变更阶段依赖")
    ids = {current[s].id for s in affected if s in current}
    result = []
    for run in runs:
        data = run.model_dump(mode="python")
        if (
            run.id in ids
            and run.task_id == task_id
            and run.status in {StageRunStatus.WAITING_CONFIRM, StageRunStatus.CONFIRMED}
            and run.validity == StageValidity.CURRENT
        ):
            data.update(
                validity=StageValidity.STALE,
                stale_at=utc_now(),
                stale_reason=reason,
                invalidated_by_change_id=change_id,
            )
        result.append(StageRun.model_validate(data))
    return result


def validate_dependencies(
    runs: list[StageRun],
    *,
    task_id: str,
    stage: InterpretationStage,
    input_refs: dict[str, str],
    dataset_revision_id: str | None = None,
) -> None:
    """消费最新已确认结果；未来 Patch 可显式选择已持久化的新 Dataset 引用。"""

    current = latest_runs(runs, task_id)
    for dependency in DEPENDENCIES[stage]:
        run = current.get(dependency)
        if (
            run is None
            or run.status != StageRunStatus.CONFIRMED
            or run.validity != StageValidity.CURRENT
        ):
            raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "前置阶段尚未确认或已经失效")
        key = OUTPUT_KEY[dependency]
        expected = (
            dataset_revision_id
            if dependency == InterpretationStage.DECODE and dataset_revision_id is not None
            else run.output_refs[key]
        )
        if not expected or input_refs.get(key) != expected:
            raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "阶段输入引用与选中结果不一致")
    if stage == InterpretationStage.INTERPRET:
        preprocess = current[InterpretationStage.PREPROCESS]
        if preprocess.input_refs.get("dataset_revision_id") != input_refs.get(
            "dataset_revision_id"
        ):
            raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "预处理结果不属于选中数据集版本")
    config_key = {
        InterpretationStage.INTERPRET: "parameter_revision_id",
        InterpretationStage.REPORT: "report_config_revision_id",
    }.get(stage)
    if config_key and not input_refs.get(config_key):
        raise WorkflowError("STAGE_DEPENDENCY_UNAVAILABLE", "阶段缺少参数或报告配置引用")
