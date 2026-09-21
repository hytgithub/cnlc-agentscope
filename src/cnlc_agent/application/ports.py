"""Adapters implement these ports; core code never owns SDK connections."""

from contextlib import AbstractContextManager
from typing import Protocol

from cnlc_agent.domain.models import Contract, JsonObject, WellData, WellId
from cnlc_agent.domain.state import InterpretationState


class WellRepository(Protocol):
    async def load(self, well_id: str) -> WellData: ...


class TaskRepository(Protocol):
    async def create(self, state: InterpretationState) -> None: ...

    async def get_report(self, task_id: str) -> str | None: ...

    async def save(self, state: InterpretationState, markdown: str) -> None: ...

    async def get(self, task_id: str) -> InterpretationState | None: ...


class InterpretationStateStore(Protocol):
    async def save(self, state: InterpretationState) -> None: ...

    async def get(self, task_id: str) -> InterpretationState | None: ...


class ModelRequest(Contract):
    task_id: str
    trace_id: str
    well_id: WellId
    purpose: str
    context: JsonObject


class ModelGateway(Protocol):
    async def generate(self, request: ModelRequest) -> JsonObject: ...


class Telemetry(Protocol):
    def span(self, name: str, attributes: JsonObject) -> AbstractContextManager[None]: ...

    def event(self, name: str, attributes: JsonObject) -> None: ...
