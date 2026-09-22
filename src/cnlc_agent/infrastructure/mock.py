"""显式的进程内 Demo 适配器，不可冒充 PostgreSQL 或 Redis 持久化。"""

import asyncio
import json
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError as SchemaError

from cnlc_agent.application.ports import ModelRequest
from cnlc_agent.domain.errors import DataError, InfrastructureError, ModelError
from cnlc_agent.domain.models import JsonObject, MockFixture, TaskRequest
from cnlc_agent.domain.state import InterpretationState


class FixtureRepository(Protocol):
    """从演示资料源读取 MockFixture 的最小端口。"""

    async def load(self, well_id: str) -> MockFixture: ...


class MockWellRepository:
    """仅允许从配置目录读取按井号命名的 JSON Fixture。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    async def load(self, well_id: str) -> MockFixture:
        """校验井号和路径边界后，异步读取并验证演示井资料。"""

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
    """回放 Fixture 预设响应，不调用 LLM、网络或专业计算。"""

    def __init__(self, repository: FixtureRepository) -> None:
        self.repository = repository

    async def generate(self, request: ModelRequest) -> JsonObject:
        """按 purpose 读取对应预设结果，保持与真实模型网关相同接口。"""

        fixture = await self.repository.load(request.well_id)
        if request.purpose == "validation":
            return fixture.validation.model_dump(mode="json")
        result = fixture.outputs.get(request.purpose)
        if result is None:
            raise ModelError("MOCK_RESPONSE_MISSING", f"缺少预设模型响应：{request.purpose}")
        return result.model_dump(mode="json")


class InMemoryStateStore:
    """仅供 Demo/测试；通过深拷贝防止调用方污染已保存快照。"""

    def __init__(self) -> None:
        self._states: dict[str, InterpretationState] = {}

    async def save(self, state: InterpretationState) -> None:
        self._states[state.task.task_id] = state.model_copy(deep=True)

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None


class InMemoryTaskRepository:
    """仅供 Demo/测试；进程退出后任务状态和报告全部丢失。"""

    def __init__(self) -> None:
        self._states: dict[str, InterpretationState] = {}
        self.reports: dict[str, str] = {}

    async def create(self, state: InterpretationState) -> None:
        if state.task.task_id in self._states:
            raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务")
        await self.save(state, "")

    async def get_report(self, task_id: str) -> str | None:
        return self.reports.get(task_id)

    async def save(self, state: InterpretationState, markdown: str) -> None:
        self._states[state.task.task_id] = state.model_copy(deep=True)
        self.reports[state.task.task_id] = markdown

    async def get(self, task_id: str) -> InterpretationState | None:
        state = self._states.get(task_id)
        return state.model_copy(deep=True) if state is not None else None
