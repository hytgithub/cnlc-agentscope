"""把测井解释 Demo Tool 接入官方 AgentScope Agent Service。"""

from collections.abc import Awaitable, Callable
from pathlib import Path
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
from agentscope.tool import ToolBase
from fastapi import FastAPI, Request, Response
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio.connection import SSLConnection

from cnlc_agent.config.settings import AppSettings, ConnectionSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.tools import build_interpretation_tool

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
    """为每个 AgentScope 会话只注册一个高层测井解释业务 Tool。"""

    del user_id, agent_id, session_id
    return [build_interpretation_tool()]


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
    app = create_app(
        storage=storage,
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(basedir=str(workspace_dir)),
        extra_agent_tools=demo_agent_tools,
        custom_agent_cls=LoggingInterpretationDemoAgent,
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

    return app


app = create_demo_app()


def main() -> None:
    """在官方示例默认的 8000 端口启动 AgentScope 服务。"""

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
