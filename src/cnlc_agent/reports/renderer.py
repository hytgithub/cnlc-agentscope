"""Renderer contract and the single style-selection registry."""

from typing import Protocol

from cnlc_agent.reports.models import NormalizedInterpretationResult, ReportStyle


class ReportRenderer(Protocol):
    def render(self, result: NormalizedInterpretationResult) -> str: ...


def get_renderer(style: ReportStyle) -> ReportRenderer:
    """Return the renderer registered for a configured report style."""

    # Local imports keep the renderer modules independent and avoid package cycles.
    from cnlc_agent.reports.compact_renderer import CompactReportRenderer
    from cnlc_agent.reports.standard_renderer import StandardReportRenderer

    renderers: dict[ReportStyle, ReportRenderer] = {
        ReportStyle.STANDARD: StandardReportRenderer(),
        ReportStyle.COMPACT: CompactReportRenderer(),
    }
    return renderers[style]
