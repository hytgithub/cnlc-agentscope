"""会话级任务工具：共享内存仓库，隔离临时输入，保持业务服务资源按调用释放。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, ToolChunk
from pydantic import ValidationError

from cnlc_agent.application.bootstrap import build_application
from cnlc_agent.application.commands import (
    FullRerunCommand,
    GetReportCommand,
    GetStatusCommand,
    ModifyInterpretationCommand,
    TaskCommandResult,
    TaskCommands,
)
from cnlc_agent.application.execution_dispatcher import InProcessExecutionDispatcher
from cnlc_agent.application.ports import TaskRepository
from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.application.service import InterpretationTaskService
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.domain.errors import ApplicationError, InfrastructureError
from cnlc_agent.domain.execution import TERMINAL_EXECUTION_STATUSES, ExecutionStatus
from cnlc_agent.domain.inputs import InterpretationInputVersion
from cnlc_agent.domain.models import MockFixture, TaskRequest, utc_now
from cnlc_agent.domain.override import InterpretationOverride
from cnlc_agent.domain.session_binding import SessionTaskBinding, TaskSessionIdentity
from cnlc_agent.infrastructure.mock import (
    InMemoryStateStore,
    InMemoryTaskRepository,
    MockWellRepository,
)

ALLOWED_TASK_TOOLS = frozenset({
    "run_well_interpretation", "modify_well_interpretation", "rerun_well_interpretation",
    "get_interpretation_status", "get_interpretation_report",
})


class TaskCommandRunner:
    """每个会话独享实例；临时目录每次重建，数据库模式仍走现有 runtime。"""

    def __init__(
        self, settings: AppSettings | None = None,
        persistence: PersistenceSettings | None = None,
        dispatcher: InProcessExecutionDispatcher | None = None,
        connections: ConnectionSettings | None = None,
        session_identity: TaskSessionIdentity | None = None,
    ) -> None:
        self.settings = settings or AppSettings(mode="demo")
        self.persistence = persistence or PersistenceSettings()
        self.repository = InMemoryTaskRepository()
        self.state_store = InMemoryStateStore()
        self.dispatcher = dispatcher or InProcessExecutionDispatcher()
        self.connections = connections or ConnectionSettings()
        self.session_identity = session_identity
        self._lock = asyncio.Lock()
        # 仅作为进程内加速缓存；postgres-redis 模式的 canonical source 是绑定表。
        self.observed_task_ids: set[str] = set()

    @asynccontextmanager
    async def context(self, root: Path) -> AsyncIterator[InterpretationTaskService]:
        """只共享会话内存状态，模型客户端在每个同步命令结束时关闭。"""

        settings = self.settings.model_copy(update={"mock_data_dir": root})
        connections = self.connections
        if settings.model_provider != "mock" and connections.model_name != "qwen-plus":
            raise ValueError("AgentScope Demo 的 MODEL_NAME 必须为 qwen-plus")
        if self.persistence.persistence == "memory":
            service = build_application(
                settings, task_repository=self.repository, state_store=self.state_store
            )
            try:
                yield service
            finally:
                await service.close()
        else:
            async with application_runtime(settings, self.persistence, connections) as service:
                yield service

    async def run(
        self, well_id: str, instruction: str = "执行单井测井解释骨架演示"
    ) -> TaskCommandResult:
        """Fixture 入口也保存 InputVersion，以支持后续受控重跑。"""

        fixture = await MockWellRepository(self.settings.mock_data_dir).load(well_id)
        return await self.start_uploaded(fixture, instruction)

    async def start_uploaded(self, fixture: MockFixture, instruction: str) -> TaskCommandResult:
        """提交首轮 QUEUED Execution 并立即返回，实际流程在请求外运行。"""

        async with self._lock:
            with TemporaryDirectory(prefix="cnlc-submit-") as directory:
                root = Path(directory)
                async with self.context(root) as service:
                    execution = await service.prepare_initial_with_input(
                        TaskRequest(well_id=fixture.well.well_id, instruction=instruction),
                        fixture,
                    )
                    if self.session_identity is not None:
                        await service.repository.bind_task_to_session(
                            SessionTaskBinding(
                                **self.session_identity.model_dump(mode="python"),
                                task_id=execution.task_id,
                            )
                        )
                    self.observed_task_ids.add(execution.task_id)
                    result = await TaskCommands(service).project(
                        execution.task_id, execution.execution_id, "START"
                    )
            await self.dispatcher.submit(
                execution.execution_id, self._executor(execution.execution_id)
            )
            return result

    def _executor(self, execution_id: str) -> Callable[[str], Awaitable[None]]:
        """每个后台 Worker 自建服务与临时输入目录，避免跨请求复用连接。"""

        async def execute(worker_id: str) -> None:
            repository: TaskRepository | None = None
            try:
                with TemporaryDirectory(prefix="cnlc-worker-") as directory:
                    root = Path(directory)
                    async with self.context(root) as service:
                        repository = service.repository
                        await service.execute_prepared(
                            execution_id,
                            worker_id=worker_id,
                            materialize=self.materializer(root),
                            lease_seconds=self.persistence.execution_lease_seconds,
                        )
            except Exception:
                # Service 在 claim 前异常时仍需终结 QUEUED；已进入 RUNNING 时也尝试
                # 用原 Worker 身份失败终结。仓库不可用时保留持久事实，后续 lease 扫描处理。
                if repository is None and self.persistence.persistence == "memory":
                    repository = self.repository
                if repository is not None:
                    await self._fail_worker_exception(repository, execution_id, worker_id)
                raise

        return execute

    async def _fail_worker_exception(
        self, repository: TaskRepository, execution_id: str, worker_id: str
    ) -> None:
        """幂等补偿 claim 前异常；不覆盖其他 Worker 的终态或 lease。"""

        try:
            await repository.finish_execution(
                execution_id, worker_id, ExecutionStatus.FAILED,
                error_code="BACKGROUND_EXECUTION_FAILED",
            )
            return
        except InfrastructureError:
            pass
        try:
            claimed = await repository.claim_execution(
                execution_id, worker_id,
                utc_now() + timedelta(seconds=self.persistence.execution_lease_seconds),
            )
            if claimed:
                await repository.finish_execution(
                    execution_id, worker_id, ExecutionStatus.FAILED,
                    error_code="BACKGROUND_EXECUTION_FAILED",
                )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Background failure could not be persisted: type=%s", type(exc).__name__
            )

    @staticmethod
    def materializer(root: Path) -> Callable[[InterpretationInputVersion], Awaitable[None]]:
        """仅从已校验的 InputVersion 物化数据；不使用上传文件名或旧临时路径。"""

        async def materialize(version: InterpretationInputVersion) -> None:
            await asyncio.to_thread(
                (root / f"{version.well_id}.json").write_text,
                version.payload.model_dump_json(), encoding="utf-8",
            )
        return materialize

    async def execute(
        self, command: GetStatusCommand,
    ) -> TaskCommandResult:
        """修改命令只提交后台任务；状态与报告命令始终查询持久事实。"""

        if not await self.owns_task(command.task_id):
            raise ApplicationError("TASK_NOT_FOUND", "任务不属于当前会话")

        async with self._lock:
            with TemporaryDirectory(prefix="cnlc-command-") as directory:
                root = Path(directory)
                async with self.context(root) as service:
                    commands = TaskCommands(service)
                    if isinstance(command, ModifyInterpretationCommand):
                        result = await commands.prepare_modify(command)
                    elif isinstance(command, FullRerunCommand):
                        result = await commands.prepare_full_rerun(command)
                    elif isinstance(command, GetReportCommand):
                        return await commands.report(command)
                    else:
                        return await commands.status(command)
            await self.dispatcher.submit(result.execution_id, self._executor(result.execution_id))
            return result

    async def owns_task(self, task_id: str) -> bool:
        """缓存未命中时只在持久模式查询完整 Session binding。"""

        if task_id in self.observed_task_ids:
            return True
        if (
            self.persistence.persistence != "postgres-redis"
            or self.session_identity is None
        ):
            return False
        with TemporaryDirectory(prefix="cnlc-ownership-") as directory:
            async with self.context(Path(directory)) as service:
                owned = await service.repository.task_belongs_to_session(
                    self.session_identity, task_id
                )
        if owned:
            self.observed_task_ids.add(task_id)
        return owned

    async def restore_task_bindings(self) -> set[str]:
        """PostgreSQL 模式从 durable binding 恢复缓存；内存模式不伪装可恢复。"""

        if (
            self.persistence.persistence != "postgres-redis"
            or self.session_identity is None
        ):
            return set(self.observed_task_ids)
        with TemporaryDirectory(prefix="cnlc-restore-") as directory:
            async with self.context(Path(directory)) as service:
                task_ids = await service.repository.list_session_task_ids(
                    self.session_identity
                )
        self.observed_task_ids.update(task_ids)
        return set(self.observed_task_ids)

    async def wait_for_completion(
        self, task_id: str, execution_id: str, *, timeout_seconds: float = 10
    ) -> TaskCommandResult:
        """供测试和非交互兼容调用等待终态；任务级 Tool 不使用此方法阻塞请求。"""

        async with asyncio.timeout(timeout_seconds):
            return await self.wait_for_execution_completion(task_id, execution_id)

    async def wait_for_execution_completion(
        self, task_id: str, execution_id: str
    ) -> TaskCommandResult:
        """等待当前进程 Worker 结束，并以持久 Execution 终态作为完成依据。"""

        worker_error: Exception | None = None
        try:
            # dispatcher.wait 内部使用 shield；取消 SSE 等待者不会取消后台 Worker。
            await self.dispatcher.wait(execution_id)
        except Exception as exc:
            worker_error = exc
        with TemporaryDirectory(prefix="cnlc-status-") as directory:
            async with self.context(Path(directory)) as service:
                execution = await service.repository.get_execution(execution_id)
                if execution is None or execution.task_id != task_id:
                    raise InfrastructureError(
                        "EXECUTION_NOT_FOUND", "指定执行不存在或不属于当前任务"
                    )
                if execution.status not in TERMINAL_EXECUTION_STATUSES:
                    if worker_error is not None:
                        raise InfrastructureError(
                            "BACKGROUND_EXECUTION_FAILED", "后台执行未形成可读取终态"
                        ) from worker_error
                    raise InfrastructureError(
                        "EXECUTION_NOT_TERMINAL", "后台执行尚未进入终态"
                    )
                return await TaskCommands(service).project(task_id, execution_id, "STATUS")

    async def get_execution_report(
        self, task_id: str, execution_id: str
    ) -> TaskCommandResult:
        """按明确 execution_id 读取报告，避免当前版本指针变化导致串版。"""

        with TemporaryDirectory(prefix="cnlc-report-") as directory:
            async with self.context(Path(directory)) as service:
                return await TaskCommands(service).report(
                    GetReportCommand(task_id=task_id, execution_id=execution_id)
                )


_SAFE_ERRORS = {
    "TASK_NOT_FOUND": "请先上传井资料或开始一次解释任务。",
    "EXECUTION_NOT_FOUND": "指定执行不存在或不属于当前任务。",
    "REPORT_NOT_FOUND": "没有符合条件的历史报告。",
    "REPORT_NOT_READY": "该版本尚无可用报告。",
    "NO_EFFECTIVE_CHANGE": "参数与当前版本相同，请提供实际变化的参数。",
    "EMPTY_OVERRIDE": "请明确要修改的参数名称和值。",
    "TASK_EXECUTION_ACTIVE": "当前任务已有执行正在排队或运行，请稍后查询状态。",
    "STALE_EXECUTION_PLAN": "任务版本已变化，请重新提交修改请求。",
    "SESSION_TASK_BINDING_FAILED": "任务归属保存失败，请稍后重试。",
}


class TaskCommandTool(ToolBase):
    """严格 Schema 的任务工具公共错误与响应外壳，不允许任意命令字典。"""

    is_concurrency_safe = False
    is_read_only = False
    command_type: type[GetStatusCommand] = GetStatusCommand

    def __init__(self, runner: TaskCommandRunner) -> None:
        super().__init__()
        self.runner = runner

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        """用户主动请求的任务操作可直接执行。"""

        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW, message="执行用户请求的任务操作"
        )

    async def call(self, *args: Any, **kwargs: Any) -> ToolChunk:
        """应用异常只输出稳定代码和固定安全文案，绝不输出异常原文。"""

        try:
            if args:
                raise ValueError("keyword arguments required")
            command: GetStatusCommand
            if self.command_type is ModifyInterpretationCommand:
                command = ModifyInterpretationCommand(
                    task_id=kwargs.pop("task_id", ""),
                    changes=InterpretationOverride.model_validate(kwargs)
                )
            else:
                command = self.command_type.model_validate(kwargs)
            result = await self.runner.execute(command)
            payload = result.model_dump(mode="json")
            return ToolChunk(
                content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
                state=ToolResultState.SUCCESS,
                metadata={"result": payload},
            )
        except (ValidationError, ValueError, TypeError):
            code = "INVALID_COMMAND"
            message = "参数无效；当前只支持采样间隔、POR、PERM 和预测模型。"
        except ApplicationError as exc:
            code = exc.code if exc.code in _SAFE_ERRORS else "TASK_COMMAND_FAILED"
            message = _SAFE_ERRORS.get(code, "任务操作失败，请检查资料或服务配置。")
        except Exception as exc:
            logging.getLogger(__name__).warning("Task command failed: %s", type(exc).__name__)
            code, message = "TASK_COMMAND_FAILED", "任务操作失败，请检查资料或服务配置。"
        payload = {"error_code": code, "message": message}
        return ToolChunk(
            content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))],
            state=ToolResultState.ERROR, metadata=payload,
        )


class ModifyWellInterpretationTool(TaskCommandTool):
    """修改受支持参数，执行范围由确定性计划决定。"""

    name = "modify_well_interpretation"
    description = "修改已有任务的采样间隔、POR、PERM 或专业预测模型并重跑；至少一个实际变化。"
    command_type = ModifyInterpretationCommand
    input_schema = {
        **InterpretationOverride.model_json_schema(),
        "properties": {
            "task_id": GetStatusCommand.model_json_schema()["properties"]["task_id"],
            **InterpretationOverride.model_json_schema()["properties"],
        },
        "required": ["task_id"],
    }


class RerunWellInterpretationTool(TaskCommandTool):
    """显式全量重跑任务，并保留有效参数。"""

    name = "rerun_well_interpretation"
    description = "对已有任务全部重新解释一次，保留当前有效参数。"
    command_type = FullRerunCommand
    input_schema = FullRerunCommand.model_json_schema()


class GetInterpretationStatusTool(TaskCommandTool):
    """读取持久 Execution 当前状态。"""

    name = "get_interpretation_status"
    description = "查询已有任务真实保存的后台执行状态、版本、参数和工具调用统计。"
    is_read_only = True
    input_schema = GetStatusCommand.model_json_schema()


class GetInterpretationReportTool(TaskCommandTool):
    """读取指定或选定历史版本的原始报告。"""

    name = "get_interpretation_report"
    description = (
        "读取已有任务报告，支持 CURRENT、PREVIOUS、LATEST_SUCCESSFUL 或可信 execution_id。"
    )
    command_type = GetReportCommand
    is_read_only = True
    input_schema = GetReportCommand.model_json_schema()


def build_task_tools(runner: TaskCommandRunner | None = None) -> list[ToolBase]:
    """所有工具共用一个会话 runner，不建立进程全局任务仓库。"""

    from cnlc_agent.demo.tools import RunWellInterpretationTool

    runner = runner if runner is not None else TaskCommandRunner()
    return [RunWellInterpretationTool(runner), *[
        tool(runner) for tool in (
            ModifyWellInterpretationTool, RerunWellInterpretationTool,
            GetInterpretationStatusTool, GetInterpretationReportTool,
        )
    ]]
