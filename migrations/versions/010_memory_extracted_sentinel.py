"""Add `memory_extracted_at` sentinel on conversations — Session D2.

Revision ID: 010_memory_extracted_sentinel
Revises: 009_memories
Create Date: 2026-04-24

Session D2 — socle pour l'**extraction automatique de faits durables
post-conversation**. La colonne `memory_extracted_at` est une **sentinelle
one-shot** qui empêche de ré-exécuter le job arq d'extraction plusieurs
fois sur la même conversation.

Pattern aligné sur `title_generated_at` (B5 auto-title) :
- Tant que `NULL` → la conv est candidate à l'extraction.
- Dès que le worker a tourné (même si 0 fait extrait) → posé à `NOW()`.
- Plus jamais retraité.

**Index partiel** pour le cron fallback futur (Phase 12) qui scannera les
conversations qui ont dépassé le seuil mais dont l'enqueue a foiré
(Redis flap, worker down). Le cron prendra ces rows et re-enqueuera :

    ix_conversations_memory_pending (updated_at)
        WHERE memory_extracted_at IS NULL
          AND deleted_at IS NULL
          AND message_count >= 6

Rollback : DROP INDEX + DROP COLUMN. Aucun impact sur la Feature Chat
existante (colonne nullable, pas de valeur par défaut à backfiller).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010_memory_extracted_sentinel"
down_revision = "009_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "memory_extracted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # Cron fallback Phase 12 : identifier les conversations qui auraient
    # dû être traitées mais ne l'ont pas été (enqueue raté ou worker KO
    # entre-temps). message_count >= 6 = seuil EXTRACTION_MIN_MESSAGES.
    op.create_index(
        "ix_conversations_memory_pending",
        "conversations",
        ["updated_at"],
        postgresql_where=sa.text(
            "memory_extracted_at IS NULL AND deleted_at IS NULL AND message_count >= 6"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_memory_pending", table_name="conversations")
    op.drop_column("conversations", "memory_extracted_at")
