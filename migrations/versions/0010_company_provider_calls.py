"""持久化真实公司 Provider 的有界规范化调用事实。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    """ProviderCall 绑定 Task、Execution、InputVersion，不存路径和 GDSX bytes。"""

    op.create_table(
        "interpretation_company_provider_call",
        sa.Column("provider_call_id", sa.Text(), primary_key=True),
        sa.Column("external_call_id", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("interpretation_task.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.Text(),
            sa.ForeignKey("interpretation_execution.execution_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "input_version_id",
            sa.Text(),
            sa.ForeignKey("interpretation_input_version.input_version_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_operation", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_summary", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_result", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "provider_operation IN ('analysis', 'preprocessing', 'interpretation', 'report')",
            name="ck_company_provider_call_operation",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCESS', 'UNKNOWN', 'FAILED')",
            name="ck_company_provider_call_status",
        ),
        sa.UniqueConstraint(
            "task_id",
            "execution_id",
            "input_version_id",
            "provider_operation",
            name="uq_company_provider_call_identity_operation",
        ),
    )
    op.create_index(
        "ix_company_provider_call_execution_operation",
        "interpretation_company_provider_call",
        ["execution_id", "provider_operation"],
    )


def downgrade():
    """删除 ProviderCall 索引和表，不触碰 ToolRun 历史。"""

    op.drop_index(
        "ix_company_provider_call_execution_operation",
        table_name="interpretation_company_provider_call",
    )
    op.drop_table("interpretation_company_provider_call")
