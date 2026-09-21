"""Initial task snapshot and report persistence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "interpretation_task",
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("well_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_interpretation_task_well_id", "interpretation_task", ["well_id"])
    op.create_index("ix_interpretation_task_status", "interpretation_task", ["status"])


def downgrade():
    op.drop_table("interpretation_task")
