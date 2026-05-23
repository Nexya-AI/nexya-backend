"""Add messages.metadata_json — persistance des tool calls (planner-from-chat).

Revision ID: 025_messages_metadata
Revises: 024_yearly_range
Create Date: 2026-05-22

planner-from-chat (2026-05-22) — la carte de tâche affichée dans le chat
quand l'IA déclenche un `create_task` ne survivait pas à la réouverture
de la conversation : les tool calls vivaient uniquement en mémoire côté
Flutter, et la table `messages` n'avait aucune colonne pour les stocker.

Cette migration ajoute `metadata_json JSONB NULL` sur `messages`. Le
finalize du stream persisté y écrit, pour les messages assistant qui ont
déclenché des tools, un instantané structuré :

    {"tool_calls": [
        {"id": "...", "name": "create_task", "success": true,
         "data": {"task": {...}}, "error": null}
    ]}

Au rechargement de la conversation, le frontend reconstruit la carte de
tâche depuis ce bloc. `NULL` pour la quasi-totalité des messages (seuls
les messages assistant ayant exécuté ≥ 1 tool le renseignent).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "025_messages_metadata"
down_revision = "024_yearly_range"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "metadata_json")
