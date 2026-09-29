"""把测井解释 Demo Tool 接入官方 AgentScope Agent Service。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit

import uvicorn
from agentscope.app import create_app
from agentscope.app.access import (
    ResourceAccessPolicyBase,
    ResourceKind,
    ResourcePermission,
    ResourceRef,
)
from agentscope.app.deps import get_current_user_id
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import RedisStorage, StorageBase
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import DashScopeCredential
from agentscope.tool import ToolBase, Toolkit
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio.connection import SSLConnection

from cnlc_agent.application.execution_dispatcher import InProcessExecutionDispatcher
from cnlc_agent.application.runtime import application_runtime
from cnlc_agent.config.settings import AppSettings, ConnectionSettings, PersistenceSettings
from cnlc_agent.demo.demo_agent import (
    LoggingInterpretationDemoAgent,
    MockTaskShellCredential,
    MockTaskShellModel,
)
from cnlc_agent.demo.operation_tool import build_agent_task_tools
from cnlc_agent.demo.read_models import (
    InterpretationExecutionView,
    InterpretationTaskView,
    present_execution_view,
    present_task_view,
)
from cnlc_agent.demo.task_tools import TaskCommandRunner
from cnlc_agent.demo.uploads import GdsxUpload
from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.session_binding import TaskSessionIdentity
from cnlc_agent.infrastructure.conversation import DurableConversationStorage


class GdsxIngressResponse(BaseModel):
    """正式 multipart ingress 的小型响应，不包含文件正文。"""

    artifact_id: str
    filename: str
    size: int
    sha256: str


class GdsxTaskCreateRequest(BaseModel):
    """正式任务创建只接受 Artifact 身份和小型业务参数。"""

    artifact_id: str
    agent_id: str
    session_id: str
    instruction: str = "解编上传的 GDSX 井资料"
    well_id: str | None = None


class GdsxTaskCreateResponse(BaseModel):
    task_id: str
    execution_id: str


class RejectDurableGdsxDataBlockMiddleware:
    """正式持久模式在 ChatService 保存用户消息前拒绝 Base64 GDSX。"""

    def __init__(self, app: Any, *, enabled: bool) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self.enabled or scope.get("type") != "http" or scope.get("path") != "/chat/":
            await self.app(scope, receive, send)
            return
        body_parts: list[bytes] = []
        more = True
        while more:
            message = await receive()
            body_parts.append(message.get("body", b""))
            more = bool(message.get("more_body", False))
        body = b"".join(body_parts)
        try:
            payload = json.loads(body)
            raw_input = payload.get("input", {})
            messages = raw_input if isinstance(raw_input, list) else [raw_input]
            blocks = [
                block
                for message in messages
                if isinstance(message, dict)
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "data"
            ]
            is_gdsx = any(
                str(block.get("name", "")).casefold().endswith(".gdsx")
                or str(block.get("source", {}).get("media_type", "")).casefold()
                == "application/x-hdf5"
                for block in blocks
            )
        except (TypeError, ValueError, UnicodeError):
            is_gdsx = False
        if is_gdsx:
            response = JSONResponse(
                status_code=415,
                content={
                    "detail": "GDSX_DURABLE_DATABLOCK_UNSUPPORTED",
                    "message": "正式模式请使用 multipart GDSX ingress。",
                },
            )
            await response(scope, receive, send)
            return
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


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

    return build_agent_task_tools(
        TaskCommandRunner(
            session_identity=TaskSessionIdentity(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            )
        )
    )


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
        return build_agent_task_tools(self.runners[key])

    def _new_runner(self, user_id: str, agent_id: str, session_id: str) -> TaskCommandRunner:
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
            service = await stack.enter_async_context(
                application_runtime(self.settings, self.persistence, self.connections)
            )
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
        if toolkit and any(group.mcps or group.skills_or_loaders for group in toolkit.tool_groups):
            raise ValueError("Demo Agent 不允许注册 MCP 或技能工具")
        builtins = {
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "LS",
            "TaskCreate",
            "TaskGet",
            "TaskList",
            "TaskUpdate",
            "ToolStop",
            "ScheduleCreate",
            "ScheduleView",
            "ScheduleDelete",
            "ScheduleList",
            "TeamCreate",
            "TeamDelete",
            "TeamSay",
            "AgentCreate",
            "AgentInvite",
        }
        tools = [
            tool
            for group in (toolkit.tool_groups if toolkit else [])
            for tool in group.tools
            if not (
                type(tool).__module__.startswith("agentscope.") and type(tool).__name__ in builtins
            )
        ]
        # Mock provider 下替换 AgentScope 外层模型，避免 UI 联调依赖公网密钥；
        # 前端仍使用固定 qwen-plus 标识，专业 Workflow 的模型边界保持不变。
        if AppSettings().model_provider == "mock":
            kwargs["model"] = MockTaskShellModel()
        super().__init__(toolkit=Toolkit(tools=tools), **kwargs)


def _redis_connection_kwargs(connections: ConnectionSettings) -> dict[str, Any]:
    """解析 Redis 连接参数；返回值只在构造存储时使用，禁止写日志。"""

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
    kwargs: dict[str, Any] = {
        "host": parsed.hostname,
        "port": parsed.port or 6379,
        "db": db,
        "password": password,
        "username": username,
    }
    if parsed.scheme == "rediss":
        kwargs["connection_class"] = SSLConnection
    return kwargs


def _redis_storage(connections: ConnectionSettings) -> RedisStorage:
    """内存业务模式仍使用官方 RedisStorage，便于离线与框架级测试。"""

    return RedisStorage(**_redis_connection_kwargs(connections))


def _conversation_storage(
    connections: ConnectionSettings,
    persistence: PersistenceSettings,
) -> DurableConversationStorage:
    """正式模式构造 PostgreSQL durable + Redis cache 的 AgentScope Storage。"""

    if connections.database_url is None:
        raise ValueError("postgres-redis 模式需要 DATABASE_URL")
    database_url = connections.database_url.get_secret_value()
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL 必须使用 postgresql+asyncpg 驱动")
    return DurableConversationStorage(
        database_url,
        session_cache_ttl_seconds=persistence.session_cache_ttl_seconds,
        conversation_retention_days=persistence.conversation_retention_days,
        **_redis_connection_kwargs(connections),
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
    storage: StorageBase = (
        _conversation_storage(connections, persistence)
        if persistence.persistence == "postgres-redis"
        else _redis_storage(connections)
    )
    api_key = connections.model_api_key
    model_name = connections.model_name
    base_url = connections.model_base_url
    # 只有后端模型配置完整且名称受支持时，才向前端发布只读模型选项。
    backend_credential = (
        MockTaskShellCredential(
            id=BACKEND_MODEL_CREDENTIAL_ID,
            name="CNLC Mock Shell",
        )
        if settings.model_provider == "mock"
        else DashScopeCredential(
            id=BACKEND_MODEL_CREDENTIAL_ID,
            name="CNLC Backend Model",
            api_key=api_key,
            base_url=base_url,
        )
        if api_key is not None and base_url is not None and model_name == "qwen-plus"
        else None
    )

    # 正式模式以 PostgreSQL 保存长期会话，Redis 仅缓存活跃会话；消息总线仍只传实时事件。
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
                RejectDurableGdsxDataBlockMiddleware,
                enabled=persistence.persistence == "postgres-redis",
            ),
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        ],
        enable_scheduler=False,
        title="CNLC Well Interpretation Demo",
    )
    # 应用级持有调度器和会话工厂，供生命周期管理与只读运行状态检查。
    app.state.cnlc_task_tools = task_tools

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def execution_lifespan(application: FastAPI) -> AsyncIterator[None]:
        """在官方 lifespan 内发布模型凭证，并加入租约恢复与后台清理。"""

        async with original_lifespan(application):
            if backend_credential is not None:
                await storage.upsert_credential(
                    BACKEND_MODEL_OWNER_ID,
                    backend_credential,
                )
            await task_tools.start()
            try:
                yield
            finally:
                await task_tools.shutdown()

    app.router.lifespan_context = execution_lifespan

    @app.post(
        "/cnlc/interpretation/artifacts/gdsx",
        response_model=GdsxIngressResponse,
    )
    async def upload_gdsx_artifact(
        file: Annotated[UploadFile, File()],
        agent_id: Annotated[str, Form(min_length=1)],
        session_id: Annotated[str, Form(min_length=1)],
        user_id: str = Depends(get_current_user_id),
    ) -> GdsxIngressResponse:
        """multipart 直接进入 ArtifactStore；不会构造 AgentScope DataBlock。"""

        session = await storage.get_session(user_id, agent_id, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="SESSION_NOT_FOUND")
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > settings.max_gdsx_upload_bytes:
                raise HTTPException(status_code=413, detail="GDSX_UPLOAD_TOO_LARGE")
        await task_tools(user_id, agent_id, session_id)
        runner = task_tools.get_existing_runner(user_id, agent_id, session_id)
        assert runner is not None
        upload = GdsxUpload(
            content=bytes(content),
            filename=file.filename or "upload.gdsx",
            media_type=file.content_type or "application/octet-stream",
            instruction="multipart artifact staging",
        )
        receipt = await runner.stage_gdsx(upload)
        return GdsxIngressResponse(
            artifact_id=receipt.artifact_id,
            filename=receipt.filename,
            size=receipt.size_bytes,
            sha256=receipt.content_sha256,
        )

    @app.post(
        "/cnlc/interpretation/tasks/gdsx",
        response_model=GdsxTaskCreateResponse,
    )
    async def create_gdsx_task(
        payload: GdsxTaskCreateRequest,
        user_id: str = Depends(get_current_user_id),
    ) -> GdsxTaskCreateResponse:
        """消费 artifact_id 创建 Task；请求体不接受 bytes、Base64 或文件路径。"""

        session = await storage.get_session(user_id, payload.agent_id, payload.session_id)
        if session is None:
            # 任务入口不区分“会话不存在”与“收据不属于会话”，避免侧信道。
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND")
        await task_tools(user_id, payload.agent_id, payload.session_id)
        runner = task_tools.get_existing_runner(user_id, payload.agent_id, payload.session_id)
        assert runner is not None
        try:
            result = await runner.start_gdsx_artifact(
                payload.artifact_id, payload.instruction, payload.well_id
            )
        except InfrastructureError as exc:
            if exc.code == "ARTIFACT_NOT_FOUND":
                raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND") from None
            if exc.code == "GDSX_INGRESS_CONFLICT":
                raise HTTPException(status_code=409, detail="GDSX_INGRESS_CONFLICT") from None
            raise
        return GdsxTaskCreateResponse(
            task_id=result.task_id,
            execution_id=result.execution_id,
        )

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
