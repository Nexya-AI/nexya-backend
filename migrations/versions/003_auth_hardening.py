"""Auth hardening — device_quotas + auth_events.

Ajout de deux tables qui durcissent l'inscription et tracent les
évènements d'auth :

- `device_quotas` : UPSERT par (device_id, day UTC) pour plafonner
  le nombre d'inscriptions par appareil par jour.
- `auth_events`   : journal forensic (register/login/reset/logout/…)
  qu'on requête pour détecter brute-force + audit RGPD.

Revision ID: 003_auth_hardening
Revises: 002_chat
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "003_auth_hardening"
down_revision = "002_chat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table device_quotas ────────────────────────────────────
    # PK composite (device_id, day) — l'UPSERT
    # `ON CONFLICT (device_id, day) DO UPDATE SET count = count + 1`
    # est donc atomique, pas besoin d'un SELECT préalable.
    op.create_table(
        "device_quotas",
        sa.Column("device_id", sa.String(128), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_ip", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("device_id", "day", name="pk_device_quotas"),
        sa.CheckConstraint("count >= 0", name="ck_device_quotas_count_non_negative"),
    )

    # ── Table auth_events ──────────────────────────────────────
    op.create_table(
        "auth_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # `user_id` nullable — un register échoué sur un email inexistant
        # n'a pas d'user associé, mais on veut quand même la trace.
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("device_id", sa.String(128), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        # ON DELETE SET NULL — on conserve l'audit même si l'user est
        # anonymisé (suppression RGPD). La traçabilité prime sur la
        # suppression stricte pour les événements de sécurité.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "event_type IN ("
            "'register_success', 'register_failed', "
            "'login_success', 'login_failed', "
            "'logout', "
            "'password_change', 'password_reset_request', 'password_reset_success', "
            "'account_delete', "
            "'captcha_failed', 'device_quota_exceeded'"
            ")",
            name="ck_auth_events_event_type",
        ),
    )
    op.create_index("idx_auth_events_user_time", "auth_events", ["user_id", "created_at"])
    op.create_index("idx_auth_events_type_time", "auth_events", ["event_type", "created_at"])
    op.create_index("idx_auth_events_ip_time", "auth_events", ["ip", "created_at"])


def downgrade() -> None:
    op.drop_table("auth_events")
    op.drop_table("device_quotas")
