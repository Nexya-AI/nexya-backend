"""Create auth tables (users, refresh_tokens, device_tokens).

Revision ID: 001_auth
Revises: (initial)
Create Date: 2026-04-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "001_auth"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table users ────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("username", sa.String(50), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(10), server_default="fr", nullable=False),
        sa.Column("timezone", sa.String(50), server_default="Africa/Douala", nullable=False),
        sa.Column("plan", sa.String(20), server_default="free", nullable=False),
        sa.Column("plan_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voice_id", UUID(as_uuid=True), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("mfa_secret", sa.String(255), nullable=True),
        sa.Column("data_collection_enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    # ── Table refresh_tokens ───────────────────────────────────
    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("idx_refresh_tokens_user", "refresh_tokens", ["user_id", "revoked_at"])

    # ── Table device_tokens ────────────────────────────────────
    op.create_table(
        "device_tokens",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(500), nullable=False),
        sa.Column("platform", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("idx_device_tokens_user", "device_tokens", ["user_id", "is_active"])


def downgrade() -> None:
    op.drop_table("device_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("users")
