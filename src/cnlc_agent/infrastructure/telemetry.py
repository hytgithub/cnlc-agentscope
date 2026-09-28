"""基于日志的 Trace 适配器；OTLP 导出留待后续任务接入。"""

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Link, Status, StatusCode

from cnlc_agent.domain.models import JsonObject, utc_now

# 请求级观察器只负责转发进度，Workflow 因此无需依赖 Web 或 AgentScope。
event_observer: ContextVar[Callable[[str, JsonObject], None] | None] = ContextVar(
    "cnlc_event_observer", default=None
)
current_span_id: ContextVar[str | None] = ContextVar("cnlc_span_id", default=None)


class LoggingTelemetry:
    """同时输出机器可读 Trace 和面向演示终端的中文进度。"""

    def __init__(self) -> None:
        self.trace_logger = logging.getLogger("cnlc_agent.trace")
        self.progress_logger = logging.getLogger("cnlc_agent.progress")

    def event(self, name: str, attributes: JsonObject) -> None:
        """记录业务事件，并在当前请求存在观察器时同步推送进度。"""

        observer = event_observer.get()
        if observer is not None:
            observer(name, attributes)
        event = {"event": name, "timestamp": utc_now().isoformat(), **attributes}
        # 完整结构化事件使用 DEBUG，避免大量 JSON 淹没演示终端的阶段摘要。
        self.trace_logger.debug(json.dumps(event, ensure_ascii=False))
        message = self._progress_message(name, attributes)
        if message:
            self.progress_logger.info(message)

    @staticmethod
    def _progress_message(name: str, attributes: JsonObject) -> str | None:
        step_id = attributes.get("step_id")
        step_name = attributes.get("step_name")
        if name == "workflow.step.start" and step_id and step_name:
            return (
                f"\n{'=' * 56}\n"
                f"阶段：{step_id}｜{step_name}\n"
                f"本阶段处理内容：{attributes['step_description']}\n"
                f"数据范围：{attributes['processing_data']}\n"
                f"输入：{attributes['input_summary']}\n"
                "当前状态：开始处理\n"
                f"{'=' * 56}"
            )
        if name == "state.change" and step_id and step_name:
            return (
                f"阶段完成：{step_id}｜{step_name}\n"
                f"处理状态：{attributes['status']}\n"
                f"输出：{attributes['output_summary']}\n"
                f"处理说明：{attributes['reason']}"
            )
        if name == "workflow.step.error" and step_id and step_name:
            return f"阶段异常：{step_id}｜{step_name}\n异常类型：{attributes['error_type']}"
        if name == "workflow.result":
            return f"\n流程结束：最终状态 {attributes['status']}"
        return None

    @contextmanager
    def span(
        self,
        name: str,
        attributes: JsonObject,
        *,
        new_trace: bool = False,
    ) -> Iterator[None]:
        """同时生成业务事件和 OTel Span；Execution 可开启新 Trace 并链接提交方。"""

        start = perf_counter()
        parent_span_id = current_span_id.get()
        active_context = trace.get_current_span().get_span_context()
        links = [Link(active_context)] if new_trace and active_context.is_valid else None
        otel_context = Context() if new_trace else None
        tracer = trace.get_tracer("cnlc_agent.business")
        safe_attributes = self._otel_attributes(attributes)
        with tracer.start_as_current_span(
            f"cnlc.{name}",
            context=otel_context,
            links=links,
            attributes=safe_attributes,
        ) as otel_span:
            span_context = otel_span.get_span_context()
            span_id = (
                f"{span_context.span_id:016x}" if span_context.is_valid else uuid4().hex[:16]
            )
            token = current_span_id.set(span_id)
            correlated = {
                **attributes,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
            }
            self.event(f"{name}.start", correlated)
            try:
                yield
            except Exception as exc:
                # 只记录异常类型；第三方异常文本可能包含连接串或鉴权信息。
                otel_span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                self.event(
                    f"{name}.error",
                    {**correlated, "error_type": type(exc).__name__},
                )
                raise
            else:
                otel_span.set_status(Status(StatusCode.OK))
            finally:
                self.event(
                    f"{name}.end",
                    {
                        **correlated,
                        "duration_ms": round((perf_counter() - start) * 1000, 3),
                    },
                )
                current_span_id.reset(token)

    @staticmethod
    def _otel_attributes(attributes: JsonObject) -> dict[str, str | int | float | bool]:
        """只把标量安全元数据写入 OTel；业务快照仍由审计边界负责。"""

        return {
            f"cnlc.{key}": value
            for key, value in attributes.items()
            if isinstance(value, (str, int, float, bool))
        }
