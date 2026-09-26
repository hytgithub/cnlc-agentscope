"""长期保存 AgentScope Conversation，并把 Redis 降为活跃会话缓存。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    """创建会话和结构化消息表；会话删除只级联聊天消息。"""

    op.create_table(
        "conversation_session",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("record_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "user_id",
            "agent_id",
            "session_id",
            name="pk_conversation_session",
        ),
        sa.UniqueConstraint(
            "user_id",
            "session_id",
            name="uq_conversation_session_user_session",
        ),
    )
    op.create_index(
        "ix_conversation_session_owner",
        "conversation_session",
        ["user_id", "agent_id", "session_id"],
    )
    op.create_index(
        "ix_conversation_session_last_active",
        "conversation_session",
        ["last_active_at"],
    )

    op.create_table(
        "conversation_message",
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "agent_id", "session_id"],
            [
                "conversation_session.user_id",
                "conversation_session.agent_id",
                "conversation_session.session_id",
            ],
            name="fk_conversation_message_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", name="pk_conversation_message"),
        sa.UniqueConstraint(
            "user_id",
            "agent_id",
            "session_id",
            "sequence",
            name="uq_conversation_message_session_sequence",
        ),
    )
    op.create_index(
        "ix_conversation_message_session_sequence",
        "conversation_message",
        ["user_id", "agent_id", "session_id", "sequence"],
    )


def downgrade():
    """只回退 Conversation 表，不删除既有测井业务事实。"""

    op.drop_index(
        "ix_conversation_message_session_sequence",
        table_name="conversation_message",
    )
    op.drop_table("conversation_message")
    op.drop_index(
        "ix_conversation_session_last_active",
        table_name="conversation_session",
    )
    op.drop_index(
        "ix_conversation_session_owner",
        table_name="conversation_session",
    )
    op.drop_table("conversation_session")
