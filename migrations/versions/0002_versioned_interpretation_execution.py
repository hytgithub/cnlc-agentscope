"""为持续任务增加版本化 Execution，并迁移 0001 已保存的当前快照。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    """先建版本表，再回填旧任务，最后设置当前/最近成功指针。"""

    op.add_column(
        "interpretation_task", sa.Column("current_execution_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "interpretation_task",
        sa.Column("latest_successful_execution_id", sa.Text(), nullable=True),
    )
    op.create_table(
        "interpretation_execution",
        sa.Column("execution_id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_execution_task_sequence"),
    )
    op.create_index("ix_interpretation_execution_task_id", "interpretation_execution", ["task_id"])
    # 0001 的合法快照均含 workflow_execution_id；保留旧数据作为各任务的初始版本。
    op.execute(
        """
        INSERT INTO interpretation_execution
            (execution_id, task_id, sequence, status, state_snapshot,
             markdown, trigger_type, created_at, updated_at)
        SELECT snapshot ->> 'workflow_execution_id', task_id, 1, status, snapshot,
               markdown, 'INITIAL', created_at, updated_at
        FROM interpretation_task
        WHERE snapshot ? 'workflow_execution_id'
        """
    )
    op.execute(
        """
        UPDATE interpretation_task
        SET current_execution_id = snapshot ->> 'workflow_execution_id',
            latest_successful_execution_id = CASE
                WHEN status IN ('SUCCESS', 'WARNING') AND markdown <> ''
                THEN snapshot ->> 'workflow_execution_id'
                ELSE NULL
            END
        WHERE snapshot ? 'workflow_execution_id'
        """
    )


def downgrade():
    """恢复 0001 表结构；其当前版本兼容快照仍留在 Task 行。"""

    op.drop_index("ix_interpretation_execution_task_id", table_name="interpretation_execution")
    op.drop_table("interpretation_execution")
    op.drop_column("interpretation_task", "latest_successful_execution_id")
    op.drop_column("interpretation_task", "current_execution_id")
