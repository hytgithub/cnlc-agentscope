"""把测井解释 Demo Tool 接入官方 AgentScope Agent Service。"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import unquote, urlsplit

import uvicorn
from agentscope.app import create_app
from agentscope.app.access import (
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourcePermission,
    ResourceRef,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import RedisStorage, StorageBase
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import DashScopeCredential
from agentscope.tool import ToolBase, Toolkit
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio.connection import SSLConnection

from cnlc_agent.application.execution_dispatcher import InProcessExecutionDispatcher
from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.read_models import (
    InterpretationExecutionView,
    InterpretationTaskView,
    present_execution_view,
    present_task_view,
)
from cnlc_agent.demo.task_tools import TaskCommandRunner, build_task_tools
from cnlc_agent.domain.session_binding import TaskSessionIdentity

BACKEND_MODEL_CREDENTIAL_ID = "cnlc-backend-model"
BACKEND_MODEL_OWNER_ID = "__cnlc_backend_model__"


class BackendModelAccessPolicy(ResourceAccessPolicyBase):
    """向前端共享服务端模型凭证的只读引用，但不暴露密钥。"""

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    async def list_accessible(
        self,
        viewer_id: str,
        kind: ResourceKind,
        storage: StorageBase,
    ) -> list[ResourceRef]:
        """仅允许普通用户读取后端托管凭证，凭证所有者无需重复授权。"""

        del storage
        if (
            not self._enabled
            or viewer_id == BACKEND_MODEL_OWNER_ID
            or kind is not ResourceKind.CREDENTIAL
        ):
            return []
        return [
            ResourceRef(
                kind=ResourceKind.CREDENTIAL,
                owner_id=BACKEND_MODEL_OWNER_ID,
                resource_id=BACKEND_MODEL_CREDENTIAL_ID,
                permission=ResourcePermission.READ,
            ),
        ]


async def demo_agent_tools(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> list[ToolBase]:
    """独立调用时创建一组隔离任务工具；HTTP 使用下方应用级会话工厂。"""

    return build_task_tools(TaskCommandRunner(session_identity=TaskSessionIdentity(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )))


class SessionTaskToolFactory:
    """应用实例内按完整会话身份隔离 runner，跨 HTTP 请求保留内存任务。"""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        persistence: PersistenceSettings | None = None,
        connections: ConnectionSettings | None = None,
        dispatcher: InProcessExecutionDispatcher | None = None,
    ) -> None:
        self.settings = settings or AppSettings(mode="demo")
        self.persistence = persistence or PersistenceSettings()
        self.connections = connections or ConnectionSettings()
        self.dispatcher = dispatcher or InProcessExecutionDispatcher()
        self.runners: dict[tuple[str, str, str], TaskCommandRunner] = {}
        self._runner_lock = asyncio.Lock()
        self._recovery_stack: AsyncExitStack | None = None
        self._recovery_task: asyncio.Task[None] | None = None

    async def __call__(self, user_id: str, agent_id: str, session_id: str) -> list[ToolBase]:
        """创建会话 runner，并在持久模式先恢复历史任务归属。"""

        key = (user_id, agent_id, session_id)
        async with self._runner_lock:
            if key not in self.runners:
                runner = self._new_runner(user_id, agent_id, session_id)
                await runner.restore_task_bindings()
                self.runners[key] = runner
        return build_task_tools(self.runners[key])

    def _new_runner(
        self, user_id: str, agent_id: str, session_id: str
    ) -> TaskCommandRunner:
        """集中构造携带完整 Session identity 的 runner。"""

        return TaskCommandRunner(
            self.settings,
            self.persistence,
            self.dispatcher,
            self.connections,
            session_identity=TaskSessionIdentity(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            ),
        )

    def get_existing_runner(
        self, user_id: str, agent_id: str, session_id: str
    ) -> TaskCommandRunner | None:
        """只查找既有会话 runner；只读 API 不得隐式创建会话状态。"""

        return self.runners.get((user_id, agent_id, session_id))

    async def get_or_restore_runner(
        self, user_id: str, agent_id: str, session_id: str
    ) -> TaskCommandRunner | None:
        """Read API 可恢复持久 runner；内存模式仍只承认进程内已有实例。"""

        key = (user_id, agent_id, session_id)
        existing = self.runners.get(key)
        if existing is not None:
            return existing
        if self.persistence.persistence != "postgres-redis":
            return None
        async with self._runner_lock:
            existing = self.runners.get(key)
            if existing is not None:
                return existing
            runner = self._new_runner(user_id, agent_id, session_id)
            if not await runner.restore_task_bindings():
                return None
            self.runners[key] = runner
            return runner

    async def start(self) -> None:
        """PostgreSQL 模式启动时回收过期 RUNNING；内存会话没有跨进程状态。"""

        if self.persistence.persistence != "postgres-redis":
            return
        stack = AsyncExitStack()
        try:
            service = await stack.enter_async_context(application_runtime(
                self.settings, self.persistence, self.connections
            ))
            await self.dispatcher.recover(service.repository)
        except BaseException:
            await stack.aclose()
            raise
        self._recovery_stack = stack

        async def poll_expired() -> None:
            """运行中进程也识别失联 Worker；只改变过期 Execution 的状态。"""

            while True:
                await asyncio.sleep(self.persistence.execution_poll_seconds)
                try:
                    await self.dispatcher.recover(service.repository)
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "Execution lease recovery failed: type=%s", type(exc).__name__
                    )

        self._recovery_task = asyncio.create_task(poll_expired(), name="execution-lease-recovery")

    async def shutdown(self) -> None:
        """统一停止应用级调度器，避免每个会话各自遗留后台协程。"""

        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._recovery_task
            self._recovery_task = None
        try:
            await self.dispatcher.shutdown()
        finally:
            if self._recovery_stack is not None:
                await self._recovery_stack.aclose()
                self._recovery_stack = None


class AgentScopeServiceAdapter(LoggingInterpretationDemoAgent):
    """移除官方服务自动注入的通用工具，再进入严格任务工具校验。"""

    def __init__(self, *, toolkit: Toolkit | None = None, **kwargs: Any) -> None:
        # 上游 get_toolkit 无开关，固定注入工作区/调度/团队工具；它们不进入业务 Agent。
        # 只识别上游已知类，其他自定义、MCP 或专业工具必须由父类拒绝。
        if toolkit and any(
            group.mcps or group.skills_or_loaders for group in toolkit.tool_groups
        ):
            raise ValueError("Demo Agent 不允许注册 MCP 或技能工具")
        builtins = {
            "Bash", "Read", "Write", "Edit", "Glob", "Grep", "LS", "TaskCreate", "TaskGet",
            "TaskList", "TaskUpdate", "ToolStop", "ScheduleCreate", "ScheduleView",
            "ScheduleDelete", "ScheduleList", "TeamCreate", "TeamDelete", "TeamSay",
            "AgentCreate", "AgentInvite",
        }
        tools = [
            tool for group in (toolkit.tool_groups if toolkit else []) for tool in group.tools
            if not (type(tool).__module__.startswith("agentscope.")
                    and type(tool).__name__ in builtins)
        ]
        super().__init__(toolkit=Toolkit(tools=tools), **kwargs)


def _redis_storage(connections: ConnectionSettings) -> RedisStorage:
    """解析 REDIS_URL 并创建 AgentScope 会话存储，不输出连接凭据。"""

    raw_url = (
        connections.redis_url.get_secret_value()
        if connections.redis_url is not None
        else "redis://localhost:6379/0"
    )
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"redis", "rediss"} or parsed.hostname is None:
        raise ValueError("AgentScope Agent Service 需要有效的 redis:// 或 rediss:// URL")
    try:
        db = int(parsed.path.lstrip("/") or "0")
    except ValueError:
        raise ValueError("REDIS_URL 的数据库编号必须为整数") from None
    password = unquote(parsed.password) if parsed.password is not None else None
    username = unquote(parsed.username) if parsed.username is not None else None
    if parsed.scheme == "rediss":
        return RedisStorage(
            host=parsed.hostname,
            port=parsed.port or 6379,
            db=db,
            password=password,
            username=username,
            connection_class=SSLConnection,
        )
    return RedisStorage(
        host=parsed.hostname,
        port=parsed.port or 6379,
        db=db,
        password=password,
        username=username,
    )


def create_demo_app(
    *,
    connections: ConnectionSettings | None = None,
    workspace_dir: Path | None = None,
) -> FastAPI:
    """创建 AgentScope FastAPI 服务；真正的网络连接由应用生命周期管理。"""

    settings = AppSettings()
    persistence = PersistenceSettings()
    connections = connections or ConnectionSettings()
    workspace_dir = workspace_dir or settings.output_dir / "agentscope_workspaces"
    storage = _redis_storage(connections)
    api_key = connections.model_api_key
    model_name = connections.model_name
    base_url = connections.model_base_url
    # 只有后端模型配置完整且名称受支持时，才向前端发布只读模型选项。
    backend_credential = (
        DashScopeCredential(
            id=BACKEND_MODEL_CREDENTIAL_ID,
            name="CNLC Backend Model",
            api_key=api_key,
            base_url=base_url,
        )
        if api_key is not None and base_url is not None and model_name == "qwen-plus"
        else None
    )

    # 聊天会话和消息落入 Redis；消息总线只负责当前进程中的实时事件分发。
    task_tools = SessionTaskToolFactory(
        settings=settings.model_copy(update={"mode": "demo"}),
        persistence=persistence,
        connections=connections,
    )
    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(basedir=str(workspace_dir)),
        extra_agent_tools=task_tools,
        custom_agent_cls=AgentScopeServiceAdapter,
        resource_access_policy=BackendModelAccessPolicy(
            enabled=backend_credential is not None,
        ),
        extra_middlewares=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            )
        ],
        enable_scheduler=False,
        title="CNLC Well Interpretation Demo",
    )
    # 应用级持有调度器和会话工厂，供生命周期管理与只读运行状态检查。
    app.state.cnlc_task_tools = task_tools

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def execution_lifespan(application: FastAPI) -> AsyncIterator[None]:
        """在官方 lifespan 内加入租约恢复和后台任务清理。"""

        async with original_lifespan(application):
            await task_tools.start()
            try:
                yield
            finally:
                await task_tools.shutdown()

    app.router.lifespan_context = execution_lifespan

    @app.middleware("http")
    async def provision_backend_model(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """确保每次请求前后端托管凭证已存在，前端无需配置 API Key。"""

        if backend_credential is not None:
            await storage.upsert_credential(
                BACKEND_MODEL_OWNER_ID,
                backend_credential,
            )
        return await call_next(request)

    async def owned_runner(
        request: Request, agent_id: str, session_id: str, task_id: str
    ) -> TaskCommandRunner:
        """用完整会话身份定位任务；所有不匹配统一返回 404，避免泄露存在性。"""

        user_id = request.headers.get("X-User-ID", "")
        if not user_id:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
        runner = await task_tools.get_or_restore_runner(user_id, agent_id, session_id)
        if runner is None or not await runner.owns_task(task_id):
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
        return runner

    @app.get(
        "/cnlc/interpretation/agents/{agent_id}/sessions/{session_id}/tasks/{task_id}",
        response_model=InterpretationTaskView,
    )
    async def get_interpretation_task(
        request: Request, agent_id: str, session_id: str, task_id: str
    ) -> InterpretationTaskView:
        """读取当前会话拥有的持续任务和执行历史，不触发 Agent 或模型。"""

        runner = await owned_runner(request, agent_id, session_id, task_id)
        with TemporaryDirectory(prefix="cnlc-read-") as directory:
            async with runner.context(Path(directory)) as service:
                task = await service.repository.get_task(task_id)
                if task is None:
                    raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
                return await present_task_view(service.repository, task)

    @app.get(
        "/cnlc/interpretation/agents/{agent_id}/sessions/{session_id}/tasks/{task_id}"
        "/executions/{execution_id}",
        response_model=InterpretationExecutionView,
    )
    async def get_interpretation_execution(
        request: Request,
        agent_id: str,
        session_id: str,
        task_id: str,
        execution_id: str,
    ) -> InterpretationExecutionView:
        """读取指定历史版本；执行标识还必须属于 URL 中的任务。"""

        runner = await owned_runner(request, agent_id, session_id, task_id)
        with TemporaryDirectory(prefix="cnlc-read-") as directory:
            async with runner.context(Path(directory)) as service:
                execution = await service.repository.get_execution(execution_id)
                if execution is None or execution.task_id != task_id:
                    raise HTTPException(status_code=404, detail="EXECUTION_NOT_FOUND")
                return await present_execution_view(service.repository, execution)

    return app


app = create_demo_app()


def main() -> None:
    """在官方示例默认的 8000 端口启动 AgentScope 服务。"""

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
