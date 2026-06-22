"""Add `lifecycle_emails` table — journal idempotent des emails de cycle de vie.

Revision ID: 030_lifecycle_emails
Revises: 029_user_avatar_storage_key
Create Date: 2026-06-22 (Phase 3 — emails lifecycle : onboarding drip, puis
réengagement + digest qui réutiliseront cette même table).

Pourquoi cette table ?

  Les emails « lifecycle » (envoyés proactivement par cron, pas en réaction à
  une action user) doivent être **idempotents** : on ne veut JAMAIS envoyer
  deux fois le même email d'onboarding J3 au même user (même si le cron tourne
  plusieurs fois, ou rejoue un jour manqué).

  `email_key` (VARCHAR libre, PAS de CHECK) garde la table **extensible** : les
  futures séquences (réengagement `reengagement_d7`, digest hebdo
  `digest_2026_W25`...) plugent leur clé sans nouvelle migration.

  UNIQUE (user_id, email_key) = garantie d'unicité au niveau DB. Le service
  « claim » un slot via INSERT ON CONFLICT DO NOTHING avant d'envoyer.

FK ON DELETE CASCADE : un user supprimé (RGPD) emporte son journal lifecycle.
created_at (via UUIDMixin) = horodatage d'envoi.

Rollback strict inverse : DROP TABLE.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "030_lifecycle_emails"
down_revision = "029_user_avatar_storage_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_emails",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email_key", sa.String(length=64), nullable=False),
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
        sa.UniqueConstraint("user_id", "email_key", name="uq_lifecycle_emails_user_key"),
    )
    # Analytics / cron lookup par type d'email (volumes, derniers envois).
    op.create_index(
        "ix_lifecycle_emails_key_time",
        "lifecycle_emails",
        ["email_key", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_lifecycle_emails_key_time", table_name="lifecycle_emails")
    op.drop_table("lifecycle_emails")
