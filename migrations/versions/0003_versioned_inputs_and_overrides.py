"""增加规范化输入版本及每次执行的 Override 快照。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    """旧 Execution 没有可证实的上传来源，输入引用保持 NULL。"""

    op.add_column(
        "interpretation_task", sa.Column("current_input_version_id", sa.Text(), nullable=True)
    )
    op.create_table(
        "interpretation_input_version",
        sa.Column("input_version_id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("well_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_input_version_task_sequence"),
    )
    op.create_index(
        "ix_interpretation_input_version_task_id", "interpretation_input_version", ["task_id"]
    )
    op.add_column(
        "interpretation_execution",
        sa.Column(
            "input_version_id",
            sa.Text(),
            sa.ForeignKey("interpretation_input_version.input_version_id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "interpretation_execution",
        sa.Column(
            "override_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade():
    """先移除执行引用，再删除输入版本表。"""

    op.drop_column("interpretation_execution", "override_snapshot")
    op.drop_column("interpretation_execution", "input_version_id")
    op.drop_index(
        "ix_interpretation_input_version_task_id", table_name="interpretation_input_version"
    )
    op.drop_table("interpretation_input_version")
    op.drop_column("interpretation_task", "current_input_version_id")
