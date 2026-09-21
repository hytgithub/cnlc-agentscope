"""Log-based trace adapter; OTLP exporting is a later task."""

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from cnlc_agent.domain.models import JsonObject, utc_now

# Optional request-local observer; the workflow stays independent of Web/AgentScope.
event_observer: ContextVar[Callable[[str, JsonObject], None] | None] = ContextVar(
    "cnlc_event_observer", default=None
)


class LoggingTelemetry:
    def __init__(self) -> None:
        self.trace_logger = logging.getLogger("cnlc_agent.trace")
        self.progress_logger = logging.getLogger("cnlc_agent.progress")

    def event(self, name: str, attributes: JsonObject) -> None:
        observer = event_observer.get()
        if observer is not None:
            observer(name, attributes)
        event = {"event": name, "timestamp": utc_now().isoformat(), **attributes}
        # Keep the complete machine-readable trace for debugging without obscuring the demo console.
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
    def span(self, name: str, attributes: JsonObject) -> Iterator[None]:
        start = perf_counter()
        self.event(f"{name}.start", attributes)
        try:
            yield
        except Exception as exc:
            # Record the class only: exception messages can contain credentials.
            self.event(f"{name}.error", {**attributes, "error_type": type(exc).__name__})
            raise
        finally:
            self.event(
                f"{name}.end",
                {
                    **attributes,
                    "duration_ms": round((perf_counter() - start) * 1000, 3),
                },
            )
