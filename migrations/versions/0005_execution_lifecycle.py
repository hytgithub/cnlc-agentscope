"""为 Execution 增加后台生命周期、合法起点和 Worker lease。"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    """旧终态保留可推断的起点；遗留 PENDING/RUNNING 明确失败而不恢复。"""

    op.add_column("interpretation_execution", sa.Column("start_step", sa.String(8)))
    op.add_column("interpretation_execution", sa.Column("source_execution_id", sa.Text()))
    op.add_column(
        "interpretation_execution",
        sa.Column("planning_reason", sa.String(32), nullable=False, server_default="INITIAL"),
    )
    op.add_column(
        "interpretation_execution", sa.Column("started_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "interpretation_execution", sa.Column("finished_at", sa.DateTime(timezone=True))
    )
    op.add_column("interpretation_execution", sa.Column("lease_owner", sa.String(128)))
    op.add_column(
        "interpretation_execution", sa.Column("lease_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column("interpretation_execution", sa.Column("error_code", sa.String(128)))
    op.execute(
        """
        UPDATE interpretation_execution
        SET start_step = CASE
                WHEN jsonb_array_length(COALESCE(state_snapshot->'reused_steps', '[]'::jsonb))
                    >= 10 THEN NULL
                WHEN jsonb_array_length(COALESCE(state_snapshot->'reused_steps', '[]'::jsonb))
                    >= 3 THEN 'W04'
                WHEN jsonb_array_length(COALESCE(state_snapshot->'reused_steps', '[]'::jsonb))
                    >= 1 THEN 'W02'
                ELSE 'W01'
            END,
            planning_reason = 'LEGACY_MIGRATED',
            finished_at = updated_at
        WHERE status NOT IN ('PENDING', 'RUNNING')
        """
    )
    op.execute(
        """
        UPDATE interpretation_execution
        SET status = 'FAILED',
            start_step = 'W01',
            planning_reason = 'LEGACY_MIGRATED',
            finished_at = updated_at,
            error_code = 'LEGACY_EXECUTION_INCOMPLETE'
        WHERE status IN ('PENDING', 'RUNNING')
        """
    )
    op.create_index(
        "ix_interpretation_execution_lease_expires_at",
        "interpretation_execution",
        ["lease_expires_at"],
    )


def downgrade():
    """移除 0005 新增索引和列；既有 Execution 快照与报告仍保留。"""

    op.drop_index(
        "ix_interpretation_execution_lease_expires_at",
        table_name="interpretation_execution",
    )
    for column in (
        "error_code",
        "lease_expires_at",
        "lease_owner",
        "finished_at",
        "started_at",
        "planning_reason",
        "source_execution_id",
        "start_step",
    ):
        op.drop_column("interpretation_execution", column)
