"""异步 PostgreSQL 仓库；单行原子保存任务快照与最终报告。"""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import DateTime, String, Text, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cnlc_agent.domain.errors import InfrastructureError
from cnlc_agent.domain.models import JsonObject
from cnlc_agent.domain.state import InterpretationState


class Base(DeclarativeBase):
    """SQLAlchemy 声明式模型基类。"""

    pass


class TaskRow(Base):
    """解释任务最小持久化表，Schema 稳定前暂不拆分专业结果子表。"""

    __tablename__ = "interpretation_task"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    well_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    snapshot: Mapped[JsonObject] = mapped_column(JSONB)
    markdown: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PostgreSQLTaskRepository:
    """TaskRepository 的 PostgreSQL 实现。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create(self, state: InterpretationState) -> None:
        """创建 PENDING 任务；主键冲突明确转换为 TASK_EXISTS。"""

        try:
            async with self.sessions.begin() as session:
                session.add(
                    TaskRow(
                        task_id=state.task.task_id,
                        well_id=state.task.well_id,
                        status=state.status.value,
                        snapshot=state.model_dump(mode="json"),
                        markdown="",
                        created_at=state.created_at,
                        updated_at=state.updated_at,
                    )
                )
        except IntegrityError:
            raise InfrastructureError("TASK_EXISTS", "任务标识已存在，请创建新任务") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库任务创建失败") from None

    async def save(self, state: InterpretationState, markdown: str) -> None:
        """在同一数据库事务中更新状态快照和 Markdown 报告。"""

        try:
            async with self.sessions.begin() as session:
                saved = await session.scalar(
                    update(TaskRow)
                    .where(TaskRow.task_id == state.task.task_id)
                    .values(
                        status=state.status.value,
                        snapshot=state.model_dump(mode="json"),
                        markdown=markdown,
                        updated_at=state.updated_at,
                    )
                    .returning(TaskRow.task_id)
                )
                if saved is None:
                    raise InfrastructureError("TASK_NOT_FOUND", "任务尚未创建")
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_WRITE_FAILED", "数据库状态保存失败") from None

    async def get(self, task_id: str) -> InterpretationState | None:
        """按任务标识读取并重新校验完整 InterpretationState。"""

        try:
            async with self.sessions() as session:
                payload = await session.scalar(
                    select(TaskRow.snapshot).where(TaskRow.task_id == task_id)
                )
            return InterpretationState.model_validate(payload) if payload is not None else None
        except ValidationError:
            raise InfrastructureError("INVALID_STORED_STATE", "数据库状态结构无效") from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库状态读取失败") from None

    async def get_report(self, task_id: str) -> str | None:
        """读取最终或诊断 Markdown；空字符串表示报告尚未生成。"""

        try:
            async with self.sessions() as session:
                result = await session.scalar(
                    select(TaskRow.markdown).where(TaskRow.task_id == task_id)
                )
            return str(result) if result is not None else None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise InfrastructureError("DATABASE_READ_FAILED", "数据库报告读取失败") from None
