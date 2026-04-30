"""Create chat tables (conversations, messages, abuse_reports).

Revision ID: 002_chat
Revises: 001_auth
Create Date: 2026-04-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "002_chat"
down_revision = "001_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table conversations ────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column(
            "id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column("expert_id", sa.String(32), server_default="general", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_favorite", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("title_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "char_length(expert_id) > 0", name="ck_conversations_expert_id_not_empty"
        ),
        sa.CheckConstraint(
            "message_count >= 0", name="ck_conversations_message_count_non_negative"
        ),
    )
    op.create_index(
        "idx_conversations_user_time",
        "conversations",
        ["user_id", "deleted_at", "last_message_at"],
    )
    op.create_index(
        "idx_conversations_user_favorite",
        "conversations",
        ["user_id", "last_message_at"],
        postgresql_where=sa.text("is_favorite = true AND deleted_at IS NULL"),
    )

    # ── Table messages ─────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column(
            "id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(16), server_default="completed", nullable=False),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_messages_role"),
        sa.CheckConstraint(
            "status IN ('streaming', 'completed', 'failed', 'cancelled')",
            name="ck_messages_status",
        ),
    )
    op.create_index(
        "idx_messages_conv_time",
        "messages",
        ["conversation_id", "created_at", "id"],
    )

    # ── Table abuse_reports ────────────────────────────────────
    op.create_table(
        "abuse_reports",
        sa.Column(
            "id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("reviewer_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "message_id", name="uq_abuse_reports_user_message"),
        sa.CheckConstraint(
            "reason IN ('offensive', 'dangerous', 'illegal', 'harassment', "
            "'misinformation', 'other')",
            name="ck_abuse_reports_reason",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'reviewed', 'dismissed', 'action_taken')",
            name="ck_abuse_reports_status",
        ),
    )
    op.create_index(
        "idx_abuse_reports_status_created",
        "abuse_reports",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_abuse_reports_user_created",
        "abuse_reports",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    # Ordre inverse : abuse_reports référence messages et conversations,
    # messages référence conversations.
    op.drop_table("abuse_reports")
    op.drop_table("messages")
    op.drop_table("conversations")
