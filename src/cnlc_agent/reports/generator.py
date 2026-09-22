"""先统一归一化业务事实，再按配置的模板样式渲染。"""

from cnlc_agent.domain.state import InterpretationState
from cnlc_agent.reports.models import NormalizedInterpretationResult, ReportStyle
from cnlc_agent.reports.normalizer import normalize_interpretation_result
from cnlc_agent.reports.renderer import get_renderer


class ReportGenerator:
    """隔离事实归一化与表现层渲染，保证不同模板使用同一份数据。"""

    def __init__(self, style: ReportStyle = ReportStyle.STANDARD) -> None:
        self.style = style

    def normalize(self, state: InterpretationState) -> NormalizedInterpretationResult:
        """把 Workflow State 投影为与模板无关的只读报告 DTO。"""

        return normalize_interpretation_result(state)

    def render(
        self,
        result: NormalizedInterpretationResult,
        style: ReportStyle | None = None,
    ) -> str:
        """使用指定样式或默认样式渲染已归一化结果。"""

        return get_renderer(style or self.style).render(result)

    def generate(self, state: InterpretationState) -> str:
        """完成一次归一化和渲染。"""

        return self.render(self.normalize(state))
