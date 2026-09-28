"""把持久化 ToolRun 投影为阶段页面可展示的有界审计摘要。"""

import json
from datetime import datetime
from typing import Self, cast

from pydantic import Field, JsonValue, model_validator

from cnlc_agent.domain.enums import StepId
from cnlc_agent.domain.models import Contract, JsonObject
from cnlc_agent.domain.stages import STAGE_STEPS, InterpretationStage
from cnlc_agent.domain.tool_run import ToolExecutionMode, ToolRun, ToolRunStatus
from cnlc_agent.tools.audit import bounded

MAX_STAGE_TOOL_RUNS = 50
MAX_TOOL_VIEW_BYTES = 32 * 1024
_FORBIDDEN_FIELDS = {
    "raw_data",
    "processed_data",
    "depths",
    "curves",
    "values",
    "curve_values",
    "payload",
    "mockfixture",
    "markdown",
    "candidate_markdown",
    "report",
    "token",
    "authorization",
}
_FORBIDDEN_NORMALIZED = {item.replace("_", "") for item in _FORBIDDEN_FIELDS}
_STEP_STAGE = {
    step: stage for stage, steps in STAGE_STEPS.items() for step in steps
}


class StageToolRunView(Contract):
    """单次 Tool 调用的小型展示事实；来源是 ToolRun 的有界审计快照。"""

    tool_run_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    tool_code: str = Field(min_length=1, max_length=128)
    step_id: StepId
    business_stage: InterpretationStage
    status: ToolRunStatus
    execution_mode: ToolExecutionMode
    source: str = Field(min_length=1, max_length=256)
    external_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=256)
    input_summary: JsonObject = Field(default_factory=dict)
    output_summary: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def protect_view_boundary(self) -> Self:
        """直接构造 DTO 时也拒绝大数据字段、敏感字段和无界内容。"""

        def walk(value: JsonValue, depth: int = 0) -> None:
            if depth > 5:
                raise ValueError("stage tool run projection nesting is too deep")
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.replace("_", "").casefold() in _FORBIDDEN_NORMALIZED:
                        raise ValueError(f"stage tool run projection forbids field: {key}")
                    walk(child, depth + 1)
            elif isinstance(value, list):
                if len(value) > 50:
                    raise ValueError("stage tool run projection list is too large")
                for child in value:
                    walk(child, depth + 1)
            elif isinstance(value, str) and len(value) > 1000:
                raise ValueError("stage tool run projection text is too large")

        payload = cast(JsonValue, self.model_dump(mode="json"))
        walk(payload)
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_TOOL_VIEW_BYTES:
            raise ValueError("stage tool run projection is too large")
        return self


def business_stage_for_step(step_id: StepId) -> InterpretationStage:
    """业务阶段只从 Task 11A 的 STAGE_STEPS 推导，不在数据库重复保存。"""

    return _STEP_STAGE[step_id]


def _safe_snapshot(snapshot: JsonObject) -> JsonObject:
    """对既有有界审计快照再做展示级过滤，不透传完整 output_snapshot。"""

    value = bounded(snapshot)
    if not isinstance(value, dict):
        return {}

    def clean(item: JsonValue) -> JsonValue:
        if isinstance(item, dict):
            return {
                key: clean(child)
                for key, child in item.items()
                if key.replace("_", "").casefold() not in _FORBIDDEN_NORMALIZED
            }
        if isinstance(item, list):
            return [
                clean(child)
                for child in item
                if not (
                    isinstance(child, str)
                    and child.replace("_", "").casefold() in _FORBIDDEN_NORMALIZED
                )
            ]
        return item

    cleaned = clean(value)
    return cleaned if isinstance(cleaned, dict) else {}


def stage_tool_run_view(run: ToolRun) -> StageToolRunView:
    """只使用 ToolRun 持久事实生成展示行，不加载原数据或 provider 响应。"""

    duration_ms = None
    if run.finished_at is not None:
        duration_ms = max(0, round((run.finished_at - run.started_at).total_seconds() * 1000))
    safe_error = bounded(run.error_message) if run.error_message is not None else None
    return StageToolRunView(
        tool_run_id=run.tool_run_id,
        execution_id=run.execution_id,
        tool_code=run.tool_code,
        step_id=run.step_id,
        business_stage=business_stage_for_step(run.step_id),
        status=run.status,
        execution_mode=run.execution_mode,
        source=str(bounded(run.source)),
        external_call_id=run.source_external_call_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=duration_ms,
        error_code=run.error_code,
        error_message=str(safe_error) if isinstance(safe_error, str) else None,
        input_summary=_safe_snapshot(run.input_snapshot),
        output_summary=_safe_snapshot(run.output_snapshot),
    )


def project_stage_tool_runs(
    runs: list[ToolRun], stage: InterpretationStage
) -> list[StageToolRunView]:
    """按 Step 过滤当前业务阶段；REPORT 没有步骤，因此自然返回空列表。"""

    allowed = set(STAGE_STEPS[stage])
    return [
        stage_tool_run_view(run)
        for run in runs
        if run.step_id in allowed
    ][:MAX_STAGE_TOOL_RUNS]
