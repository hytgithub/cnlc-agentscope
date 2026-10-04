"""最终回复可引用的不可变业务证据合同。"""

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import ConfigDict, Field, model_validator

from cnlc_agent.domain.execution import ExecutionStatus
from cnlc_agent.domain.models import Contract, utc_now


class ResponseStatus(StrEnum):
    """回复门控状态；它不是后台 Execution 生命周期的替代品。"""

    SUCCESS = "SUCCESS"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEED_CLARIFICATION = "NEED_CLARIFICATION"
    UNSUPPORTED = "UNSUPPORTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ResponseOperation(StrEnum):
    """描述已发生的交互结果类型，不承担操作规划职责。"""

    QUERY = "QUERY"
    START = "START"
    MODIFY = "MODIFY"
    FULL_RERUN = "FULL_RERUN"
    STATUS = "STATUS"
    REPORT_READ = "REPORT_READ"
    REPORT_GENERATION = "REPORT_GENERATION"
    STAGE_CONFIRM = "STAGE_CONFIRM"
    OTHER = "OTHER"


class ResponseSourceType(StrEnum):
    """最终回复证据来源；REAL 只有在所有执行证据明确支持时才使用。"""

    REAL = "REAL"
    MOCK = "MOCK"
    FIXTURE = "FIXTURE"
    DERIVED = "DERIVED"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ResponseEvidenceKind(StrEnum):
    """可追溯的业务证据类型。"""

    TASK = "TASK"
    EXECUTION = "EXECUTION"
    INPUT_VERSION = "INPUT_VERSION"
    TOOL_RUN = "TOOL_RUN"
    STAGE_RUN = "STAGE_RUN"
    REPORT = "REPORT"
    INTERACTION = "INTERACTION"


class ResponseEvidenceRef(Contract):
    """指向权威记录的最小引用，不复制专业结果正文。"""

    model_config = ConfigDict(frozen=True)

    kind: ResponseEvidenceKind
    reference_id: str = Field(min_length=1)
    source_type: ResponseSourceType | None = None


class ResponseScopeEvidence(Contract):
    """已由服务端解析的范围投影；空 references 表示明确的全井范围。"""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1)
    references: tuple[str, ...] = ()
    description: str = Field(min_length=1)
    top_depth: float | None = None
    bottom_depth: float | None = None
    depth_unit: str | None = None


class ResponseEvidenceEnvelope(Contract):
    """供服务端 renderer 使用的不可变事实合同，不是第二份 OperationPlan。"""

    model_config = ConfigDict(frozen=True)

    response_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    task_id: str | None = None
    well_id: str | None = None
    well_name: str | None = None
    execution_id: str | None = None
    scope: ResponseScopeEvidence | None = None
    input_version_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    operation: ResponseOperation
    status: ResponseStatus
    execution_status: ExecutionStatus | None = None
    source_type: ResponseSourceType = ResponseSourceType.UNKNOWN
    evidence_refs: tuple[ResponseEvidenceRef, ...] = ()
    result_summary: str = ""
    error_code: str | None = None
    clarification: str | None = None
    report_markdown: str | None = None
    confirmed_stage: str | None = None
    confirmed_stage_run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_success_evidence(self) -> "ResponseEvidenceEnvelope":
        """业务成功必须绑定匹配的 Task / Execution；报告成功另须有报告引用。"""

        if self.status != ResponseStatus.SUCCESS:
            return self
        kinds = {ref.kind for ref in self.evidence_refs}
        if self.operation == ResponseOperation.OTHER:
            if ResponseEvidenceKind.INTERACTION not in kinds:
                raise ValueError("SUCCESS interaction requires an application evidence ref")
            return self
        if (
            self.task_id is None
            or self.execution_id is None
            or ResponseEvidenceKind.TASK not in kinds
            or ResponseEvidenceKind.EXECUTION not in kinds
            or not any(
                ref.kind == ResponseEvidenceKind.TASK and ref.reference_id == self.task_id
                for ref in self.evidence_refs
            )
            or not any(
                ref.kind == ResponseEvidenceKind.EXECUTION and ref.reference_id == self.execution_id
                for ref in self.evidence_refs
            )
        ):
            raise ValueError("SUCCESS requires matching Task and Execution evidence refs")
        if (
            self.operation
            in {
                ResponseOperation.REPORT_READ,
                ResponseOperation.REPORT_GENERATION,
            }
            and ResponseEvidenceKind.REPORT not in kinds
        ):
            raise ValueError("report SUCCESS requires a persisted report evidence ref")
        if (
            self.operation == ResponseOperation.STAGE_CONFIRM
            and self.confirmed_stage == "REPORT"
            and ResponseEvidenceKind.REPORT not in kinds
        ):
            raise ValueError("confirmed REPORT success requires a persisted report evidence ref")
        return self

    @classmethod
    def without_result(
        cls,
        *,
        operation: ResponseOperation = ResponseOperation.OTHER,
        status: ResponseStatus = ResponseStatus.FAILED,
        result_summary: str = "",
        error_code: str | None = None,
        clarification: str | None = None,
    ) -> "ResponseEvidenceEnvelope":
        """缺少 Application 结果时生成无成功证据的安全响应合同。"""

        if status == ResponseStatus.SUCCESS:
            raise ValueError("没有 Application evidence 时禁止构造 SUCCESS")
        return cls(
            operation=operation,
            status=status,
            result_summary=result_summary,
            error_code=error_code,
            clarification=clarification,
        )


def response_operation_for_command(command: str) -> ResponseOperation:
    """将现有命令投影到回复类型，不引入新的业务动作。"""

    return {
        "START": ResponseOperation.START,
        "MODIFY": ResponseOperation.MODIFY,
        "FULL_RERUN": ResponseOperation.FULL_RERUN,
        "STATUS": ResponseOperation.STATUS,
        "GET_REPORT": ResponseOperation.REPORT_READ,
        "CONFIRM": ResponseOperation.STAGE_CONFIRM,
    }.get(command, ResponseOperation.OTHER)


def response_status_for_execution(status: ExecutionStatus) -> ResponseStatus:
    """用实际 Execution 状态门控回复语义。"""

    if status == ExecutionStatus.QUEUED:
        return ResponseStatus.QUEUED
    if status in {ExecutionStatus.RUNNING, ExecutionStatus.WAITING_CONFIRMATION}:
        return ResponseStatus.RUNNING
    if status in {ExecutionStatus.SUCCESS, ExecutionStatus.WARNING}:
        return ResponseStatus.SUCCESS
    return ResponseStatus.FAILED


def response_envelope_from_task_result(
    result: object,
    *,
    operation: ResponseOperation | None = None,
    scope: ResponseScopeEvidence | None = None,
) -> ResponseEvidenceEnvelope:
    """只从 TaskCommandResult 字段构造 envelope；不接受模型生成的事实。"""

    command = str(result.command)
    execution_status = result.execution_status
    if not isinstance(execution_status, ExecutionStatus):
        execution_status = ExecutionStatus(execution_status)
    resolved_operation = operation or response_operation_for_command(command)
    response_status = response_status_for_execution(execution_status)
    report = getattr(result, "report_markdown", None)
    report_ready = bool(getattr(result, "report_ready", False))
    if resolved_operation == ResponseOperation.REPORT_READ and not report:
        response_status = ResponseStatus.FAILED
    refs = tuple(getattr(result, "evidence_refs", ()))
    if not refs:
        # 兼容尚未带新字段的只读投影，但不会把此回退用于 SUCCESS。
        task_id = str(result.task_id)
        execution_id = str(result.execution_id)
        refs = (
            ResponseEvidenceRef(kind=ResponseEvidenceKind.TASK, reference_id=task_id),
            ResponseEvidenceRef(kind=ResponseEvidenceKind.EXECUTION, reference_id=execution_id),
        )
    if not any(ref.kind == ResponseEvidenceKind.EXECUTION for ref in refs):
        response_status = ResponseStatus.FAILED
    summary = str(getattr(result, "summary", ""))
    if execution_status == ExecutionStatus.BLOCKED:
        missing = getattr(result, "missing_data", ())
        steps = sorted(
            {str(item.get("affected_step")) for item in missing if item.get("affected_step")}
        )
        current_step = getattr(result, "current_step", None)
        current_step = getattr(current_step, "value", current_step)
        steps = list(dict.fromkeys(([str(current_step)] if current_step else []) + steps))
        detail = "、".join(steps)
        summary += " 缺少资料。" + (f"受影响步骤：{detail}。" if detail else "")
    elif execution_status == ExecutionStatus.REVIEW_REQUIRED:
        summary += " 当前需要人工复核。"
    elif execution_status == ExecutionStatus.WARNING:
        summary += f" 存在 warning（{getattr(result, 'warning_count', 0)} 项）。"
    return ResponseEvidenceEnvelope(
        task_id=result.task_id,
        well_id=result.well_id,
        well_name=getattr(result, "well_name", None),
        execution_id=result.execution_id,
        scope=scope or ResponseScopeEvidence(kind="WHOLE_WELL", description="全井解释范围"),
        input_version_id=getattr(result, "input_version_id", None),
        revision=getattr(result, "execution_sequence", None),
        operation=resolved_operation,
        status=response_status,
        execution_status=execution_status,
        source_type=getattr(result, "source_type", ResponseSourceType.UNKNOWN),
        evidence_refs=refs,
        result_summary=summary,
        error_code=getattr(result, "error_code", None),
        report_markdown=report if report_ready else None,
        confirmed_stage=getattr(result, "confirmed_stage", None),
        confirmed_stage_run_id=getattr(result, "confirmed_stage_run_id", None),
    )


def response_scope_from_resolved_scope(value: object) -> ResponseScopeEvidence:
    """只将 Resolver 返回的已绑定范围投影到最终回复。"""

    scope = value.scope
    kind = getattr(getattr(scope, "kind", None), "value", None) or str(
        getattr(scope, "kind", "UNKNOWN")
    )
    intervals = tuple(getattr(value, "intervals", ()))
    if intervals:
        references = tuple(f"{item.execution_id}:{item.interval_id}" for item in intervals)
        description = "、".join(f"第 {item.ordinal} 层" for item in intervals)
        return ResponseScopeEvidence(
            kind=kind,
            references=references,
            description=description,
        )
    coverage = getattr(value, "depth_coverage", None)
    raw = scope.model_dump(mode="json") if hasattr(scope, "model_dump") else {}
    interval_ids = raw.get("interval_ids") or [raw.get("interval_id")]
    interval_ids = [item for item in interval_ids if item]
    if interval_ids:
        return ResponseScopeEvidence(
            kind=kind,
            references=tuple(f"{value.execution_id}:{item}" for item in interval_ids),
            description="指定层段范围",
        )
    top = raw.get("top")
    bottom = raw.get("bottom")
    if top is not None and bottom is not None:
        return ResponseScopeEvidence(
            kind=kind,
            references=(f"{value.execution_id}:{kind}",),
            description=f"深度 {top}–{bottom} m",
            top_depth=top,
            bottom_depth=bottom,
            depth_unit="m",
        )
    if raw.get("depth") is not None:
        return ResponseScopeEvidence(
            kind=kind,
            references=(f"{value.execution_id}:{kind}:{raw['depth']}",),
            description=(
                f"深度点 {raw['depth']} {raw.get('unit', 'm')} ({raw.get('depth_reference', 'MD')})"
            ),
            top_depth=raw["depth"],
            bottom_depth=raw["depth"],
            depth_unit=raw.get("unit", "m"),
        )
    if raw.get("filter_expression"):
        return ResponseScopeEvidence(
            kind=kind,
            references=tuple(
                f"{value.execution_id}:{item}" for item in (raw.get("resolved_ids") or [])
            ),
            description=f"筛选范围：{raw['filter_expression']}",
        )
    if coverage is not None:
        description = f"全井覆盖范围 {coverage.top}–{coverage.bottom} {coverage.unit}"
    else:
        description = "全井解释范围" if kind == "WHOLE_WELL" else kind
    return ResponseScopeEvidence(
        kind=kind,
        references=(f"{value.execution_id}:{kind}",),
        description=description,
    )
