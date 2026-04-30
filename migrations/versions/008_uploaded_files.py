"""Create `uploaded_files` table — Session E3.

Revision ID: 008_uploaded_files
Revises: 007_library
Create Date: 2026-04-24

Table générique pour les uploads utilisateurs (PDFs, DOCX, images, audios…)
via `POST /files/upload`. Sert de **buffer** entre l'upload physique (MinIO)
et l'attachement à une feature amont (Project Files via `upload_id`, futur
Memory Documents pour RAG, etc.).

Choix architecturaux :

- **Dédup par `(user_id, content_sha256)` UNIQUE partiel** : un même user
  qui re-upload le même fichier obtient la même row (pas d'erreur, pas de
  duplicate storage). Le partiel `WHERE deleted_at IS NULL` permet la
  ré-insertion après soft-delete.

- **`attached_to_kind` + `attached_to_id` en info forensic, pas FK stricte** :
  l'upload peut être consommé par plusieurs tables (project_files,
  library_items, memory_documents). Une FK polymorphe serait un anti-pattern
  en SQL — on garde juste une trace informative pour l'audit + le cron
  de cleanup futur (supprimer les uploads non-attachés > 24 h).

- **`virus_scan_status` + `extraction_status` séparés** : deux processus
  indépendants avec leur propre cycle de vie. Un upload peut être
  `virus_scan='clean'` + `extraction_status='failed'` (PDF corrompu mais
  non-malveillant) — on garde le fichier pour l'user, juste sans texte
  indexé.

- **`extracted_text TEXT NULL`** pour permettre D4 RAG de l'indexer sans
  nouveau requery sur MinIO. Cap applicatif 500k chars côté service, la DB
  accepte bien plus (TEXT Postgres = illimité jusqu'à 1 Go).

Rollback strict inverse.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "008_uploaded_files"
down_revision = "007_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "uploaded_files",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=True),
        sa.Column("extension", sa.String(16), nullable=True),
        # Virus scan
        sa.Column(
            "virus_scan_status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("virus_scan_signature", sa.String(128), nullable=True),
        sa.Column("virus_scan_scanner", sa.String(32), nullable=True),  # 'mock' / 'clamav' / 'noop'
        sa.Column("virus_scanned_at", sa.DateTime(timezone=True), nullable=True),
        # Text extraction
        sa.Column(
            "extraction_status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extracted_text_length", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "extraction_truncated",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        # Attachment tracking (forensic, pas FK)
        sa.Column("attached_to_kind", sa.String(32), nullable=True),
        sa.Column("attached_to_id", UUID(as_uuid=True), nullable=True),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        # Soft-delete + timestamps
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_uploaded_files_size_non_negative"),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_uploaded_files_sha256_length",
        ),
        sa.CheckConstraint(
            "virus_scan_status IN ('pending','clean','suspicious','failed','skipped')",
            name="ck_uploaded_files_virus_status",
        ),
        sa.CheckConstraint(
            "extraction_status IN ('pending','ok','empty','unsupported','failed','skipped')",
            name="ck_uploaded_files_extraction_status",
        ),
        sa.CheckConstraint(
            "extracted_text_length IS NULL OR extracted_text_length >= 0",
            name="ck_uploaded_files_text_length",
        ),
        sa.CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_uploaded_files_page_count",
        ),
        sa.CheckConstraint(
            "attached_to_kind IS NULL OR attached_to_kind IN ("
            "'project_file','library_item','memory_document')",
            name="ck_uploaded_files_attached_kind",
        ),
    )

    # ── Index partiels ────────────────────────────────────────────

    # Listing user — actifs uniquement, tri récence DESC.
    op.create_index(
        "ix_uploaded_files_user_active",
        "uploaded_files",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Dédup naturelle cross-feature par contenu.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_uploaded_files_user_sha_active
            ON uploaded_files (user_id, content_sha256)
            WHERE deleted_at IS NULL
        """
    )

    # Cron cleanup futur — uploads orphelins (non-attachés) > 24 h.
    op.create_index(
        "ix_uploaded_files_user_pending",
        "uploaded_files",
        ["user_id", "created_at"],
        postgresql_where=sa.text("attached_at IS NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_uploaded_files_user_pending", table_name="uploaded_files")
    op.execute("DROP INDEX IF EXISTS uq_uploaded_files_user_sha_active")
    op.drop_index("ix_uploaded_files_user_active", table_name="uploaded_files")
    op.drop_table("uploaded_files")
