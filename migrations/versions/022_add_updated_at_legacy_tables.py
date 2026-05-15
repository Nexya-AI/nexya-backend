"""Add `updated_at` column to 4 legacy tables (Bug-015 fix — RGPD export).

Revision ID: 022_legacy_updated_at
Revises: 021_consent_log_updated_at
Create Date: 2026-05-14

Suite du Bug-014 (migration 021) : audit complet des modèles ORM héritant
de `UUIDMixin` révèle 4 autres tables où la migration originelle a oublié
de créer la colonne `updated_at` exigée par le mixin :

- `voice_transcriptions` (migration 012 — E1 Voice 2026-04-24)
- `vision_analyses` (migration 013 — E2 Vision 2026-04-24)
- `user_suggestions` (migration 018 — N1 Suggestions 2026-04-27)
- `helpdesk_escalations` (migration 019 — N4 Helpdesk 2026-04-27)

Conséquence : tout SELECT * sur ces tables échoue
`psycopg.errors.UndefinedColumn: column XXX.updated_at does not exist`.
Le bug a explosé d'abord côté `DataExportService.build_export` (RGPD K1
ZIP archive) qui consomme les 4 tables, mais touche aussi tous les
endpoints CRUD de ces features (POST /voice/transcribe, POST /vision/analyze,
POST /suggestions, helpdesk admin metrics).

Fix : ALTER TABLE ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
sur les 4 tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "022_legacy_updated_at"
down_revision = "021_consent_log_updated_at"
branch_labels = None
depends_on = None

_TABLES = (
    "voice_transcriptions",
    "vision_analyses",
    "user_suggestions",
    "helpdesk_escalations",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "updated_at")
