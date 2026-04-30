"""Create `voice_transcriptions` table — Session E1 (Voice Pro-only).

Revision ID: 012_voice_transcriptions
Revises: 011_document_chunks
Create Date: 2026-04-24

Session E1 — Voice (Whisper STT + OpenAI TTS) **Pro only**.

Stratégie coût :
- **Free** = STT/TTS natif Flutter (`speech_to_text` + `flutter_tts`),
  zéro backend → $0 de coût backend.
- **Pro** = Whisper API backend pour qualité premium + features
  exclusives (fichiers longs, langue auto 99 dialects, sauvegarde
  historique, sources RAG).

Cette table trace uniquement les transcriptions **Pro**. Free ne
touche jamais le backend voice.

Choix architecturaux :

- **Dédup SHA-256** UNIQUE partielle `(user_id, content_sha256) WHERE
  deleted_at IS NULL` — même pattern Library C3 / Memory D1 / Files E3.
  Re-transcrire un audio identique → retourne la ligne existante sans
  rappeler Whisper (économie directe facture OpenAI).

- **`model` + `provider` + `cost_usd` tracés par row** — permet à
  Ivan de mesurer a posteriori « combien coûte Whisper vs
  faster-whisper sur 6 mois » via
  `SELECT SUM(cost_usd), model FROM voice_transcriptions GROUP BY model`.
  Le switch futur vers un provider self-hosted (faster-whisper sur GPU
  Hetzner) est **mesurable avant décision**.

- **FK `source_file_id` ON DELETE SET NULL** — si l'user a d'abord
  uploadé son audio via `/files/upload` puis appelé `/voice/transcribe`
  avec l'upload_id, on trace le lien. Un soft-delete du fichier ne
  purge pas sa transcription (la transcription garde sa valeur même si
  l'audio source est supprimé).

Rollback : inverse strict.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "012_voice_transcriptions"
down_revision = "011_document_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_transcriptions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_file_id", UUID(as_uuid=True), nullable=True),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("transcribed_text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(8), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(8, 3), nullable=False),
        sa.Column("model", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default="0",
        ),
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
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_voice_sha256_length",
        ),
        sa.CheckConstraint(
            "duration_seconds >= 0",
            name="ck_voice_duration_non_negative",
        ),
        sa.CheckConstraint("cost_usd >= 0", name="ck_voice_cost_non_negative"),
    )

    # Index actif user pour listings (phase front future).
    op.create_index(
        "ix_voice_user_active",
        "voice_transcriptions",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Dédup UNIQUE partielle : même user + même hash audio = 1 ligne.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_voice_user_sha_active
            ON voice_transcriptions (user_id, content_sha256)
            WHERE deleted_at IS NULL
        """
    )

    # Index par source_file_id pour retrouver la transcription d'un
    # upload donné (feature UI future).
    op.create_index(
        "ix_voice_source_file",
        "voice_transcriptions",
        ["source_file_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND source_file_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_voice_source_file", table_name="voice_transcriptions")
    op.execute("DROP INDEX IF EXISTS uq_voice_user_sha_active")
    op.drop_index("ix_voice_user_active", table_name="voice_transcriptions")
    op.drop_table("voice_transcriptions")
