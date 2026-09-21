"""Read secrets from environment, never interpolate them into alembic.ini."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from cnlc_agent.application.runtime import database_url
from cnlc_agent.config.settings import ConnectionSettings, PersistenceSettings
from cnlc_agent.infrastructure.database import Base


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    timeout = PersistenceSettings().infrastructure_timeout_seconds
    engine = create_async_engine(
        database_url(ConnectionSettings()),
        hide_parameters=True,
        connect_args={"timeout": timeout, "command_timeout": timeout},
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=database_url(ConnectionSettings()), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
