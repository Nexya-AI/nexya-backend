"""Add `updated_at` column to `consent_log` (Bug-014 fix — K1 RGPD).

Revision ID: 021_consent_log_updated_at
Revises: 020_extend_schedule_check
Create Date: 2026-05-14

Bug-014 fix — Migration 017 (Session J1, 2026-04-26) a créé la table
`consent_log` SANS la colonne `updated_at`, alors que le modèle ORM
`ConsentLog(Base, UUIDMixin)` hérite de `UUIDMixin` qui déclare
`updated_at` (cf. `app/core/database/base.py`).

Conséquence : toute requête `SELECT consent_log.*` (sur les 4 endpoints
RGPD `/rgpd/user/consent` GET/POST + `/rgpd/user/consent/{type}` DELETE)
échoue avec `psycopg.errors.UndefinedColumn: column consent_log.updated_at
does not exist` → 500 INTERNAL_ERROR côté API, l'écran K1 RGPD du Flutter
reste bloqué sur le spinner de la section « Mes consentements ».

Le modèle ORM `DeletionRequest` redéclare explicitement `created_at` ET
`updated_at` dans son corps (cf. `app/features/rgpd/models.py` lignes
116-117), donc la table `deletion_requests` créée par migration 017 a
bien les 2 colonnes — ce bug touche uniquement `consent_log`.

Fix : ALTER TABLE ADD COLUMN `updated_at` TIMESTAMPTZ NOT NULL DEFAULT
now(). Backfill implicite via DEFAULT (toutes les rows existantes
reçoivent `now()` au moment de l'ALTER, la table est vide post-K1 en
dev mais pourrait contenir des rows si J1 a été déployé en staging).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "021_consent_log_updated_at"
down_revision = "020_extend_schedule_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consent_log",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("consent_log", "updated_at")
