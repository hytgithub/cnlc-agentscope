"""Artifact metadata 与 artifact-backed InputVersion。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    """旧 InputVersion 默认迁移为 FIXTURE；GDSX bytes 永不进入 PostgreSQL。"""

    op.create_table(
        "interpretation_artifact",
        sa.Column("artifact_id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("well_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "source_artifact_id",
            sa.Text(),
            sa.ForeignKey("interpretation_artifact.artifact_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "created_from_execution_id",
            sa.Text(),
            sa.ForeignKey("interpretation_execution.execution_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "provider_call_id",
            sa.Text(),
            sa.ForeignKey(
                "interpretation_company_provider_call.provider_call_id", ondelete="RESTRICT"
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('SOURCE_GDSX', 'PROCESSED_GDSX')", name="ck_artifact_kind"),
    )
    op.create_index("ix_artifact_task", "interpretation_artifact", ["task_id"])
    op.create_index("ix_artifact_sha", "interpretation_artifact", ["content_sha256"])
    op.add_column(
        "interpretation_input_version",
        sa.Column("payload_kind", sa.String(32), nullable=False, server_default="FIXTURE"),
    )
    op.add_column(
        "interpretation_input_version", sa.Column("source_artifact_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "interpretation_input_version",
        sa.Column("dataset_manifest", postgresql.JSONB(), nullable=True),
    )
    op.alter_column("interpretation_input_version", "payload", nullable=True)
    op.create_foreign_key(
        "fk_input_version_source_artifact",
        "interpretation_input_version",
        "interpretation_artifact",
        ["source_artifact_id"],
        ["artifact_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_input_version_payload_kind",
        "interpretation_input_version",
        "(payload_kind = 'FIXTURE' AND payload IS NOT NULL AND source_artifact_id IS NULL "
        "AND dataset_manifest IS NULL) OR "
        "(payload_kind = 'GDSX_ARTIFACT' AND payload IS NULL AND source_artifact_id IS NOT NULL "
        "AND dataset_manifest IS NOT NULL)",
    )


def downgrade():
    """仅在不存在 artifact-backed 行时可安全回退。"""

    op.drop_constraint("ck_input_version_payload_kind", "interpretation_input_version")
    op.drop_constraint(
        "fk_input_version_source_artifact", "interpretation_input_version", type_="foreignkey"
    )
    op.alter_column("interpretation_input_version", "payload", nullable=False)
    op.drop_column("interpretation_input_version", "dataset_manifest")
    op.drop_column("interpretation_input_version", "source_artifact_id")
    op.drop_column("interpretation_input_version", "payload_kind")
    op.drop_index("ix_artifact_sha", table_name="interpretation_artifact")
    op.drop_index("ix_artifact_task", table_name="interpretation_artifact")
    op.drop_table("interpretation_artifact")
