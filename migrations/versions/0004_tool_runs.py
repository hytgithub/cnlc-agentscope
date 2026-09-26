"""按 Execution 保存专业 Tool 的有界审计记录。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    """ToolRun 外键绑定 Task 和 Execution；旧历史没有伪造调用记录。"""

    op.create_table(
        "interpretation_tool_run",
        sa.Column("tool_run_id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id", sa.Text(),
            sa.ForeignKey("interpretation_task.task_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "execution_id", sa.Text(),
            sa.ForeignKey("interpretation_execution.execution_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_id", sa.String(8), nullable=False),
        sa.Column("tool_code", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("output_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("source_external_call_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_interpretation_tool_run_execution_id", "interpretation_tool_run", ["execution_id"]
    )


def downgrade():
    """先移除查询索引，再删除仅由本迁移创建的审计表。"""

    op.drop_index("ix_interpretation_tool_run_execution_id", table_name="interpretation_tool_run")
    op.drop_table("interpretation_tool_run")
