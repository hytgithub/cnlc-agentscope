"""Official AgentScope Agent Service wired to the CNLC demo Tool."""

from pathlib import Path
from urllib.parse import unquote, urlsplit

import uvicorn
from agentscope.app import create_app
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.tool import ToolBase
from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio.connection import SSLConnection

from cnlc_agent.config.settings import AppSettings, ConnectionSettings
from cnlc_agent.demo.demo_agent import LoggingInterpretationDemoAgent
from cnlc_agent.demo.tools import build_interpretation_tool


async def demo_agent_tools(
    user_id: str,
    agent_id: str,
    session_id: str,
) -> list[ToolBase]:
    """Return the one business Tool for every AgentScope chat session."""

    del user_id, agent_id, session_id
    return [build_interpretation_tool()]


def _redis_storage(connections: ConnectionSettings) -> RedisStorage:
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
    """Create the AgentScope FastAPI service without connecting at import time."""

    settings = AppSettings()
    connections = connections or ConnectionSettings()
    workspace_dir = workspace_dir or settings.output_dir / "agentscope_workspaces"
    return create_app(
        storage=_redis_storage(connections),
        message_bus=InMemoryMessageBus(),
        workspace_manager=LocalWorkspaceManager(basedir=str(workspace_dir)),
        extra_agent_tools=demo_agent_tools,
        custom_agent_cls=LoggingInterpretationDemoAgent,
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


app = create_demo_app()


def main() -> None:
    """Start the AgentScope service on the official example's default port."""

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
