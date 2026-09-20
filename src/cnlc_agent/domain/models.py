"""Minimum contracts only. Pending final well-data schema.

Mock outputs are supplied by a fixture, never inferred from invented thresholds.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from cnlc_agent.domain.enums import StepId, StepStatus, ValidationStatus

WellId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
JsonObject = dict[str, JsonValue]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class TaskRequest(Contract):
    task_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    well_id: WellId
    instruction: str = Field(default="执行单井测井解释骨架演示", min_length=1)


class Well(Contract):
    well_id: WellId
    name: str
    extensions: JsonObject = Field(default_factory=dict)


class LogCurve(Contract):
    unit: str = Field(min_length=1)
    values: list[float | None]


class RawData(Contract):
    depth_unit: Literal["m"] = "m"
    depth_reference: Literal["MD"] = "MD"
    depths: list[float]
    curves: dict[str, LogCurve]
    auxiliary: JsonObject = Field(default_factory=dict)
    extensions: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_samples(self) -> Self:
        if any(b <= a for a, b in zip(self.depths, self.depths[1:], strict=False)):
            raise ValueError("depths must be strictly increasing")
        if any(len(curve.values) != len(self.depths) for curve in self.curves.values()):
            raise ValueError("curve samples must align with depths")
        return self


class ErrorDetail(Contract):
    code: str
    message: str
    retryable: bool = False
    step_id: StepId | None = None


class MissingData(Contract):
    field: str
    importance: Literal["Required", "Recommended", "Optional"]
    affected_step: StepId


class StageResult(Contract):
    status: Literal[
        StepStatus.SUCCESS,
        StepStatus.WARNING,
        StepStatus.BLOCKED,
        StepStatus.FAILED,
        StepStatus.REVIEW_REQUIRED,
    ] = StepStatus.SUCCESS
    result: JsonObject = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    recommended_action: str = "continue"
    is_mock: bool
    source: str = Field(min_length=1)


class ValidationResult(StageResult):
    validation_status: ValidationStatus
    rollback_target: Literal[StepId.W06, StepId.W07] | None = None


class DataRequirements(Contract):
    """A provisional input contract, not a professional interpretation standard."""

    required_curves: list[str] = Field(min_length=1)
    recommended_sources: list[str] = Field(default_factory=list)


class WellData(Contract):
    well: Well
    raw_data: RawData
    requirements: DataRequirements


class MockFixture(WellData):
    schema_version: Literal["0.1-skeleton"] = "0.1-skeleton"
    is_mock: Literal[True] = True
    outputs: dict[str, StageResult]
    validation: ValidationResult

    @model_validator(mode="after")
    def only_mock_results(self) -> Self:
        if not self.validation.is_mock or any(not item.is_mock for item in self.outputs.values()):
            raise ValueError("fixture outputs must be explicitly marked is_mock=true")
        return self
