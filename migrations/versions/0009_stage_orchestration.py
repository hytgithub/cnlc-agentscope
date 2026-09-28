"""持久化 Execution 的连续或分阶段确认运行模式。"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    """既有执行保持连续模式，新执行由应用层显式选择。"""

    op.add_column(
        "interpretation_execution",
        sa.Column(
            "run_mode",
            sa.String(length=32),
            nullable=False,
            server_default="CONTINUOUS",
        ),
    )


def downgrade():
    """回退只删除运行模式列。"""

    op.drop_column("interpretation_execution", "run_mode")
