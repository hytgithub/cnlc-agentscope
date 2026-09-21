"""AgentScope Agent Service adapter for the well interpretation demo."""

from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.tools import RUN_TOOL_NAME, run_well_interpretation

__all__ = [
    "LoggingInterpretationDemoAgent",
    "RUN_TOOL_NAME",
    "run_well_interpretation",
]
