"""基础设施适配器实现这些端口，核心业务不直接持有第三方 SDK 连接。"""

from contextlib import AbstractContextManager
from typing import Protocol

from cnlc_agent.domain.models import Contract, JsonObject, WellData, WellId
from cnlc_agent.domain.state import InterpretationState


class WellRepository(Protocol):
    """井资料读取端口。"""

    async def load(self, well_id: str) -> WellData: ...


class TaskRepository(Protocol):
    """解释任务及最终报告的长期存储端口。"""

    async def create(self, state: InterpretationState) -> None: ...

    async def get_report(self, task_id: str) -> str | None: ...

    async def save(self, state: InterpretationState, markdown: str) -> None: ...

    async def get(self, task_id: str) -> InterpretationState | None: ...


class InterpretationStateStore(Protocol):
    """Workflow 运行时状态快照端口。"""

    async def save(self, state: InterpretationState) -> None: ...

    async def get(self, task_id: str) -> InterpretationState | None: ...


class ModelRequest(Contract):
    """发送到统一模型网关的最小结构化请求。"""

    task_id: str
    trace_id: str
    well_id: WellId
    purpose: str
    context: JsonObject


class ModelGateway(Protocol):
    """屏蔽模型供应商和传输协议差异的访问端口。"""

    async def generate(self, request: ModelRequest) -> JsonObject: ...


class Telemetry(Protocol):
    """业务事件和耗时跨度的可观测性端口。"""

    def span(self, name: str, attributes: JsonObject) -> AbstractContextManager[None]: ...

    def event(self, name: str, attributes: JsonObject) -> None: ...
