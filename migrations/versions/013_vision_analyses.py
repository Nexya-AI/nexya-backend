"""Create `vision_analyses` table — Session E2 (Vision multimodale).

Revision ID: 013_vision_analyses
Revises: 012_voice_transcriptions
Create Date: 2026-04-24

Session E2 — Vision analyse image via LLM multimodal
(Gemini 2.0 Flash/Pro, GPT-4o, Claude Sonnet 4).

Stratégie Free vs Pro :
- **Free** : tier=`flash` imposé → `gemini-2.0-flash` (cheapest, ~$0.00018/req).
- **Pro**  : choix tier=`flash` ou `pro` → `gemini-2.0-pro` / `gpt-4o` (qualité
  supérieure pour analyses techniques : schémas, code, diagrammes, OCR fin).

Cette table trace **toutes** les analyses (Free + Pro) :
- Dédup par `(user_id, image_sha256, prompt_sha256)` UNIQUE partielle —
  ré-analyser la MÊME image avec le MÊME prompt = retour de la ligne existante
  sans rappel LLM (économie directe).
- `cost_usd` tracé par row pour benchmark portabilité a posteriori entre
  providers via `SELECT SUM(cost_usd), model FROM vision_analyses GROUP BY model`.
- FK `source_file_id` / `source_library_id` ON DELETE SET NULL — une purge
  du fichier/item source ne supprime pas l'analyse (qui reste pertinente).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "013_vision_analyses"
down_revision = "012_voice_transcriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vision_analyses",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_file_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_library_id", UUID(as_uuid=True), nullable=True),
        sa.Column("image_sha256", sa.CHAR(64), nullable=False),
        sa.Column("prompt_sha256", sa.CHAR(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("analysis_text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column(
            "tokens_input",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "tokens_output",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("image_width", sa.Integer(), nullable=True),
        sa.Column("image_height", sa.Integer(), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["uploaded_files.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_library_id"],
            ["library_items.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "char_length(image_sha256) = 64",
            name="ck_vision_image_sha256_length",
        ),
        sa.CheckConstraint(
            "char_length(prompt_sha256) = 64",
            name="ck_vision_prompt_sha256_length",
        ),
        sa.CheckConstraint("cost_usd >= 0", name="ck_vision_cost_non_negative"),
        sa.CheckConstraint(
            "tokens_input >= 0 AND tokens_output >= 0",
            name="ck_vision_tokens_non_negative",
        ),
    )

    # Dédup UNIQUE partielle : même user + même image + même prompt.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_vision_user_img_prompt_active
            ON vision_analyses (user_id, image_sha256, prompt_sha256)
            WHERE deleted_at IS NULL
        """
    )

    # Index actif user pour listings historiques.
    op.create_index(
        "ix_vision_user_active",
        "vision_analyses",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Index source_file_id pour retrouver l'analyse d'un upload donné.
    op.create_index(
        "ix_vision_source_file",
        "vision_analyses",
        ["source_file_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND source_file_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_vision_source_file", table_name="vision_analyses")
    op.drop_index("ix_vision_user_active", table_name="vision_analyses")
    op.execute("DROP INDEX IF EXISTS uq_vision_user_img_prompt_active")
    op.drop_table("vision_analyses")
