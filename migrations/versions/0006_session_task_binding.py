"""持久保存 AgentScope Session 与解释任务的归属绑定。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    """创建最小 ownership 表；一个 Session 和一个 Task 都允许多条关联。"""

    op.create_table(
        "interpretation_session_task_binding",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id",
            "agent_id",
            "session_id",
            "task_id",
            name="pk_interpretation_session_task_binding",
        ),
    )
    op.create_index(
        "ix_session_task_binding_session",
        "interpretation_session_task_binding",
        ["user_id", "agent_id", "session_id"],
    )
    op.create_index(
        "ix_session_task_binding_task_id",
        "interpretation_session_task_binding",
        ["task_id"],
    )


def downgrade():
    """先删除查询索引，再删除绑定表；不修改任务与执行数据。"""

    op.drop_index(
        "ix_session_task_binding_task_id",
        table_name="interpretation_session_task_binding",
    )
    op.drop_index(
        "ix_session_task_binding_session",
        table_name="interpretation_session_task_binding",
    )
    op.drop_table("interpretation_session_task_binding")
