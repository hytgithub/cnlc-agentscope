"""AgentScope 会话与解释任务的持久归属契约，不进入测井业务 Task 模型。"""

from datetime import datetime

from pydantic import Field

from cnlc_agent.domain.models import Contract, utc_now


class TaskSessionIdentity(Contract):
    """服务器端会话身份；三个字段共同界定任务读取和操作边界。"""

    user_id: str = Field(min_length=1, max_length=256)
    agent_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)


class SessionTaskBinding(TaskSessionIdentity):
    """一个会话曾创建或操作过一个解释任务的持久事实。"""

    task_id: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)
