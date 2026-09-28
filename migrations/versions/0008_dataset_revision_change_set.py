"""DatasetRevision 与稀疏 DatasetChangeSet 正式持久化。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    """先建 Revision，再建 ChangeSet，最后补循环方向的受限外键。"""

    op.create_table(
        "interpretation_dataset_revision",
        sa.Column("dataset_revision_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("well_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("root_input_version_id", sa.Text(), nullable=False),
        sa.Column("parent_revision_id", sa.Text(), nullable=True),
        sa.Column("change_set_id", sa.Text(), nullable=True),
        sa.Column("lineage_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_from_execution_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(parent_revision_id IS NULL AND change_set_id IS NULL) OR "
            "(parent_revision_id IS NOT NULL AND change_set_id IS NOT NULL)",
            name="ck_dataset_revision_root_or_child",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["interpretation_task.task_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["root_input_version_id"],
            ["interpretation_input_version.input_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["interpretation_dataset_revision.dataset_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_from_execution_id"],
            ["interpretation_execution.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dataset_revision_id"),
        sa.UniqueConstraint(
            "task_id", "sequence", name="uq_dataset_revision_task_sequence"
        ),
        sa.UniqueConstraint(
            "change_set_id", name="uq_dataset_revision_change_set_id"
        ),
    )
    op.create_index(
        "ix_interpretation_dataset_revision_task_id",
        "interpretation_dataset_revision",
        ["task_id"],
    )
    op.create_table(
        "interpretation_dataset_change_set",
        sa.Column("change_set_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("well_id", sa.String(length=64), nullable=False),
        sa.Column("base_revision_id", sa.Text(), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("source_execution_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("original_instruction", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "change_type = 'CURVE_SAMPLE_PATCH'",
            name="ck_dataset_change_set_type",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["interpretation_task.task_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["base_revision_id"],
            ["interpretation_dataset_revision.dataset_revision_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_execution_id"],
            ["interpretation_execution.execution_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("change_set_id"),
    )
    op.create_index(
        "ix_interpretation_dataset_change_set_task_id",
        "interpretation_dataset_change_set",
        ["task_id"],
    )
    op.create_foreign_key(
        "fk_dataset_revision_change_set",
        "interpretation_dataset_revision",
        "interpretation_dataset_change_set",
        ["change_set_id"],
        ["change_set_id"],
        ondelete="RESTRICT",
    )


def downgrade():
    """先解除循环外键，再按依赖顺序删除两张表。"""

    op.drop_constraint(
        "fk_dataset_revision_change_set",
        "interpretation_dataset_revision",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_interpretation_dataset_change_set_task_id",
        table_name="interpretation_dataset_change_set",
    )
    op.drop_table("interpretation_dataset_change_set")
    op.drop_index(
        "ix_interpretation_dataset_revision_task_id",
        table_name="interpretation_dataset_revision",
    )
    op.drop_table("interpretation_dataset_revision")
