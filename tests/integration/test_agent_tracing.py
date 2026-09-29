"""验证 AgentScope 原生 Trace 覆盖一次完整 ReAct 回复。"""

from agentscope.event import ToolResultEndEvent
from agentscope.message import UserMsg
from agentscope.middleware._tracing._attributes import OperationNameValues, SpanAttributes
from agentscope.tool import Toolkit
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cnlc_agent.config.settings import AppSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent, MockTaskShellModel
from cnlc_agent.demo.operation_tool import build_agent_task_tools
from cnlc_agent.demo.task_tools import TaskCommandRunner


async def test_agentscope_tracing_emits_agent_model_and_tool_spans(data_dir):
    """真实推进 reply_stream，避免只验证 Middleware 已注册。"""

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    runner = TaskCommandRunner(
        AppSettings(mode="demo", model_provider="mock", mock_data_dir=data_dir, _env_file=None),
        PersistenceSettings(persistence="memory", _env_file=None),
    )
    agent = LoggingInterpretationDemoAgent(
        name="demo",
        system_prompt="",
        model=MockTaskShellModel(),
        toolkit=Toolkit(tools=build_agent_task_tools(runner)),
        stream_step_delay_seconds=0,
        stream_report_chunk_delay_seconds=0,
    )

    events = [
        event
        async for event in agent.reply_stream(
            UserMsg(name="user", content="解释 WELL_MOCK_001"),
        )
    ]
    result = next(
        event for event in events if isinstance(event, ToolResultEndEvent)
    ).metadata["result"]
    await runner.wait_for_completion(result["task_id"], result["execution_id"])

    spans = exporter.get_finished_spans()
    operations = {
        span.attributes.get(SpanAttributes.GEN_AI_OPERATION_NAME)
        for span in spans
    }
    assert OperationNameValues.INVOKE_AGENT in operations
    assert OperationNameValues.CHAT in operations
    assert OperationNameValues.EXECUTE_TOOL in operations

    business_spans = {span.name: span for span in spans if span.name.startswith("cnlc.")}
    execution = business_spans["cnlc.execution"]
    workflow = business_spans["cnlc.workflow"]
    assert execution.parent is None
    assert len(execution.links) == 1
    assert workflow.parent is not None
    assert workflow.parent.trace_id == execution.context.trace_id
    assert "cnlc.workflow.step" in business_spans
    assert "cnlc.report" in business_spans
