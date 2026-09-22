"""报告生成只消费既有状态事实，不重新解释或计算专业测量值。"""

from cnlc_agent.reports.generator import ReportGenerator
from cnlc_agent.reports.models import NormalizedInterpretationResult, ReportStyle

__all__ = ["NormalizedInterpretationResult", "ReportGenerator", "ReportStyle"]
