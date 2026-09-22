"""Typed, renderer-neutral projection of one interpretation state."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ReportStyle(StrEnum):
    """Supported final report presentations."""

    STANDARD = "standard"
    COMPACT = "compact"


class ReportModel(BaseModel):
    """Immutable report DTO base so renderers cannot alter interpretation facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Measurement(ReportModel):
    value: float | str | None = None
    unit: str | None = None


class WellSummary(ReportModel):
    well_id: str
    name: str | None = None
    well_category: str | None = None
    well_type: str | None = None
    completion_date: str | None = None
    total_depth_m: float | None = None
    completion_formation: str | None = None
    target_formation: str | None = None
    structural_location: str | None = None
    logging_interval: str | None = None
    drilling_purpose: str | None = None
    wellbore_structure: str | None = None
    drilling_fluid: str | None = None
    other_notes: str | None = None


class AcquisitionCurve(ReportModel):
    name: str
    unit: str | None = None
    interval: str | None = None
    valid_samples: int = 0
    total_samples: int = 0
    purpose: str = "测井解释输入"


class InterpretedLayer(ReportModel):
    layer_id: str | None = None
    formation: str | None = None
    top_depth_m: float | None = None
    bottom_depth_m: float | None = None
    gross_thickness_m: float | None = None
    effective_thickness_m: float | None = None
    lithology: str | None = None
    reservoir_class: str | None = None
    layer_type: str | None = None
    gr: Measurement = Field(default_factory=Measurement)
    acoustic: Measurement = Field(default_factory=Measurement)
    density: Measurement = Field(default_factory=Measurement)
    resistivity: Measurement = Field(default_factory=Measurement)
    vsh: Measurement = Field(default_factory=Measurement)
    porosity: Measurement = Field(default_factory=Measurement)
    permeability: Measurement = Field(default_factory=Measurement)
    water_saturation: Measurement = Field(default_factory=Measurement)
    hydrocarbon_saturation: Measurement = Field(default_factory=Measurement)
    total_hydrocarbon: Measurement = Field(default_factory=Measurement)
    heavy_hydrocarbon: Measurement = Field(default_factory=Measurement)
    fluid_type: str | None = None
    evidence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recommendation: str | None = None


class ValidationSummary(ReportModel):
    status: str | None = None
    summary: str | None = None
    evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    recommended_action: str | None = None


class NormalizedInterpretationResult(ReportModel):
    """One factual projection shared by every report renderer."""

    status: str
    generated_date: str
    is_mock: bool
    well: WellSummary
    curves: tuple[AcquisitionCurve, ...] = ()
    curve_names: tuple[str, ...] = ()
    quality_summary: tuple[str, ...] = ()
    quality_evidence: tuple[str, ...] = ()
    preprocessing: tuple[str, ...] = ()
    geology_summary: tuple[str, ...] = ()
    mud_logging_summary: str | None = None
    core_summary: str | None = None
    well_test_summary: str | None = None
    offset_well_summary: str | None = None
    lithology_summary: tuple[str, ...] = ()
    reservoir_summary: tuple[str, ...] = ()
    fluid_summary: tuple[str, ...] = ()
    layers: tuple[InterpretedLayer, ...] = ()
    validation: ValidationSummary = Field(default_factory=ValidationSummary)
    recommendations: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
