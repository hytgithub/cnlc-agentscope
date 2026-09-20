"""Explicit, process-local demo adapters. These do not replace PostgreSQL or Redis."""

import asyncio
import json
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.domain.errors import DataError, ModelError
from cnlc_agent.domain.models import JsonObject, MockFixture, TaskRequest
from cnlc_agent.domain.state import InterpretationState


class FixtureRepository(Protocol):
    async def load(self, well_id: str) -> MockFixture: ...


class MockWellRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def load(self, well_id: str) -> MockFixture:
        try:
            TaskRequest(well_id=well_id)
        except SchemaError as exc:
            raise DataError("INVALID_WELL_ID", "井标识格式无效") from exc
        path = (self.root / f"{well_id}.json").resolve()
        if not path.is_relative_to(self.root):
            raise DataError("INVALID_WELL_PATH", "井数据文件必须位于配置的数据目录中")
        try:
            content = await asyncio.to_thread(path.read_text, encoding="utf-8")
            fixture = MockFixture.model_validate_json(content)
        except FileNotFoundError as exc:
            raise DataError("WELL_NOT_FOUND", f"找不到演示井：{well_id}") from exc
        except (SchemaError, json.JSONDecodeError, UnicodeError) as exc:
            raise DataError("INVALID_FIXTURE", "演示井数据不符合 0.1-skeleton Schema") from exc
        except OSError as exc:
            raise DataError("DATA_READ_FAILED", "无法读取演示井数据") from exc
        if fixture.well.well_id != well_id:
            raise DataError("WELL_ID_MISMATCH", "文件中的井标识与请求不一致")
        return fixture


class MockModelGateway:
    """Replay fixture responses; no LLM, network call or interpretation calculation."""

    def __init__(self, repository: FixtureRepository) -> None:
        self.repository = repository

    async def generate(self, request: ModelRequest) -> JsonObject:
        fixture = await self.repository.load(request.well_id)
        if request.purpose == "validation":
            return fixture.validation.model_dump(mode="json")
        result = fixture.outputs.get(request.purpose)
        if result is None:
            raise ModelError("MOCK_RESPONSE_MISSING", f"缺少预设模型响应：{request.purpose}")
        return result.model_dump(mode="json")


class InMemoryStateStore:
    """Demo/test only. Snapshots prevent a caller from mutating stored state."""

    def __init__(self) -> None:
        self._states: dict[str, InterpretationState] = {}

    async def save(self, state: InterpretationState) -> None:
        self._states[state.task.task_id] = state.model_copy(deep=True)

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None


class InMemoryTaskRepository:
    """Demo/test only; no persistence after process exit."""

    def __init__(self) -> None:
        self._states: dict[str, InterpretationState] = {}
        self.reports: dict[str, str] = {}

    async def save(self, state: InterpretationState, markdown: str) -> None:
        self._states[state.task.task_id] = state.model_copy(deep=True)
        self.reports[state.task.task_id] = markdown

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None
