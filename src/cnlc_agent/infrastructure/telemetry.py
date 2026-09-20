"""Log-based trace adapter; OTLP exporting is a later task."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from cnlc_agent.domain.models import JsonObject, utc_now


class LoggingTelemetry:
    def __init__(self) -> None:
        self.logger = logging.getLogger("cnlc_agent.trace")

    def event(self, name: str, attributes: JsonObject) -> None:
        self.logger.info(
            json.dumps(
                {"event": name, "timestamp": utc_now().isoformat(), **attributes},
                ensure_ascii=False,
            )
        )

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
