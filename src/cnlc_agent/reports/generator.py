"""Normalize once, then render using the configured presentation style."""

from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.models import NormalizedInterpretationResult, ReportStyle
from cnlc_agent.reports.normalizer import normalize_interpretation_result
from cnlc_agent.reports.renderer import get_renderer


class ReportGenerator:
    def __init__(self, style: ReportStyle = ReportStyle.STANDARD) -> None:
        self.style = style

    def normalize(self, state: InterpretationState) -> NormalizedInterpretationResult:
        return normalize_interpretation_result(state)

    def render(
        self,
        result: NormalizedInterpretationResult,
        style: ReportStyle | None = None,
    ) -> str:
        return get_renderer(style or self.style).render(result)

    def generate(self, state: InterpretationState) -> str:
        return self.render(self.normalize(state))
