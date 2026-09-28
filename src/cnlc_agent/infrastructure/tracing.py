"""配置 AgentScope 原生 OpenTelemetry Trace。"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_tracing(endpoint: str | None) -> bool:
    """按需配置进程级 OTLP 导出；未配置时保持 AgentScope 的低开销 no-op。"""

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        # 测试或宿主进程可能已经配置 Provider，不能覆盖其采样和导出策略。
        return True
    normalized = endpoint.strip() if endpoint else ""
    if not normalized:
        return False
    provider = TracerProvider(
        resource=Resource.create({"service.name": "cnlc-agentscope"}),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=normalized)),
    )
    trace.set_tracer_provider(provider)
    return True
