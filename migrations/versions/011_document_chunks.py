"""Create `document_chunks` table + `uploaded_files.chunks_indexed_at` sentinel.

Revision ID: 011_document_chunks
Revises: 010_memory_extracted_sentinel
Create Date: 2026-04-24

Session D4 — RAG documents : chunking + indexation pgvector.

Cette migration pose la structure cible consommée par :
- D4 : worker `index_document_chunks` qui peuple la table.
- D5 : endpoint `/rag/query` (hors scope D4) qui interroge l'index HNSW.

Choix architecturaux :

- **Sentinelle `uploaded_files.chunks_indexed_at TIMESTAMPTZ NULL`** :
  idempotence stricte du worker (re-livraison arq après crash → skip).
  Pattern miroir D2 (`memory_extracted_at`) et B5 (`title_generated_at`).

- **Vecteurs 1536 dim** : aligné D1 memories + le modèle par défaut
  OpenAI `text-embedding-3-small`. Le Mock produit aussi 1536 dim pour
  compat stricte avec cette colonne.

- **Offsets caractère `start_char_offset` / `end_char_offset`** : permet
  de retrouver la position d'un chunk dans le texte source pour le
  debugging et le surlignage futur côté Flutter.

- **`page_number` nullable** : peuplé par l'extracteur PDF via marqueurs
  `[[PAGE:N]]` injectés avant le chunking. Reste NULL pour DOCX / TXT /
  MD qui n'ont pas de notion de page fiable.

- **UNIQUE `(file_id, chunk_index)`** : garantit l'idempotence côté DB.
  Même si le worker re-livre deux chunks de même index (bug théorique),
  Postgres rejette le doublon.

- **FK `file_id` ON DELETE CASCADE** : une purge physique d'un fichier
  supprime ses chunks. Pour le soft-delete, la requête RAG filtrera via
  JOIN `uploaded_files.deleted_at IS NULL`.

- **Index HNSW `vector_cosine_ops`** : O(log N) top-K même à 10M+ chunks
  si NEXYA atteint son objectif 950k users. Paramètres défauts pgvector
  `m=16, ef_construction=64` tuneables Phase 12.

Rollback : inverse strict. Extension `vector` conservée (partagée D1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "011_document_chunks"
down_revision = "010_memory_extracted_sentinel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Sentinelle sur uploaded_files ────────────────────────
    op.add_column(
        "uploaded_files",
        sa.Column("chunks_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Index partiel pour le cron fallback Phase 12 — rattrape les fichiers
    # pour lesquels l'enqueue a échoué (Redis flap, worker down).
    op.create_index(
        "ix_uploaded_files_chunks_pending",
        "uploaded_files",
        ["updated_at"],
        postgresql_where=sa.text(
            "chunks_indexed_at IS NULL "
            "AND deleted_at IS NULL "
            "AND extraction_status = 'ok' "
            "AND mime_type IN ("
            "'application/pdf',"
            "'application/vnd.openxmlformats-officedocument.wordprocessingml.document',"
            "'text/plain',"
            "'text/markdown'"
            ")"
        ),
    )

    # ── 2. Table document_chunks ────────────────────────────────
    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("start_char_offset", sa.Integer(), nullable=False),
        sa.Column("end_char_offset", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column(
            "embedding_model",
            sa.String(64),
            nullable=False,
            server_default="text-embedding-3-small",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("file_id", "chunk_index", name="uq_document_chunks_file_idx"),
        sa.CheckConstraint("chunk_index >= 0", name="ck_document_chunks_index_non_negative"),
        sa.CheckConstraint("token_count >= 1", name="ck_document_chunks_token_count_positive"),
        sa.CheckConstraint(
            "start_char_offset >= 0",
            name="ck_document_chunks_start_offset_non_negative",
        ),
        sa.CheckConstraint(
            "end_char_offset >= start_char_offset",
            name="ck_document_chunks_offsets_ordered",
        ),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name="ck_document_chunks_page_number_positive",
        ),
    )

    # ── 3. Colonne vector(1536) en SQL brut ─────────────────────
    # Même pattern que D1 memories : SQLAlchemy Core ne connaît pas
    # `vector` nativement, on ajoute la colonne via ALTER TABLE.
    op.execute(
        """
        ALTER TABLE document_chunks
        ADD COLUMN embedding vector(1536) NOT NULL
        """
    )

    # ── 4. Index btree ──────────────────────────────────────────
    op.create_index(
        "ix_document_chunks_user_id",
        "document_chunks",
        ["user_id"],
    )
    op.create_index(
        "ix_document_chunks_file_id",
        "document_chunks",
        ["file_id"],
    )

    # ── 5. Index HNSW vectoriel ─────────────────────────────────
    # `vector_cosine_ops` — les embeddings OpenAI et le Mock sont
    # normalisés L2 (cosinus = inner product pour vecteurs unitaires).
    # Paramètres défauts pgvector `m=16, ef_construction=64`.
    op.execute(
        """
        CREATE INDEX ix_document_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.drop_index("ix_document_chunks_file_id", table_name="document_chunks")
    op.drop_index("ix_document_chunks_user_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_uploaded_files_chunks_pending", table_name="uploaded_files")
    op.drop_column("uploaded_files", "chunks_indexed_at")
    # Extension `vector` conservée (partagée — ne pas DROP).
