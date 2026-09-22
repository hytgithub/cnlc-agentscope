"""报告渲染器契约与唯一的模板样式注册表。"""

from typing import Protocol

from cnlc_agent.reports.models import NormalizedInterpretationResult, ReportStyle


class ReportRenderer(Protocol):
    """所有报告渲染器必须实现的最小接口。"""

    def render(self, result: NormalizedInterpretationResult) -> str: ...


def get_renderer(style: ReportStyle) -> ReportRenderer:
    """返回指定样式注册的渲染器。"""

    # 局部导入使两个渲染器彼此独立，并避免包初始化阶段产生循环依赖。
    from cnlc_agent.reports.compact_renderer import CompactReportRenderer
    from cnlc_agent.reports.standard_renderer import StandardReportRenderer

    renderers: dict[ReportStyle, ReportRenderer] = {
        ReportStyle.STANDARD: StandardReportRenderer(),
        ReportStyle.COMPACT: CompactReportRenderer(),
    }
    return renderers[style]
