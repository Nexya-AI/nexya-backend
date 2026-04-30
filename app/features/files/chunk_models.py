"""
Modèle ORM DocumentChunk — D4 RAG documents.

Un chunk = fragment de texte d'un document (PDF / DOCX / TXT / MD) produit
par le chunker (`app/features/files/chunker.py`) et indexé via embeddings
OpenAI (ou Mock) dans une colonne `vector(1536)`.

Discipline :
- **Pas de relation `UploadedFile.chunks`** — anti-N+1 systématique, le
  consommateur RAG (D5) utilisera des requêtes SQL explicites JOIN.
- **Offsets caractère stockés** : `start_char_offset` et `end_char_offset`
  pointent dans le texte pré-nettoyé. Permettent le debugging et le
  surlignage futur côté Flutter.
- **`page_number` nullable** : peuplé uniquement pour les PDFs via
  marqueurs `[[PAGE:N]]` posés par l'extracteur. DOCX / TXT / MD n'ont
  pas de notion de page fiable → None.
- **`embedding_model` tracé sur chaque row** : forensic + backfill futur
  (si on migre `3-small` → `3-large` on saura quelles rows re-indexer).
- **UNIQUE `(file_id, chunk_index)`** : sécurité DB contre une double
  insertion si le worker re-livre (bug théorique, sentinelle
  `chunks_indexed_at` couvre déjà le cas nominal).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


class DocumentChunk(Base):
    """Fragment d'un document uploadé, indexé sémantiquement en pgvector.

    Ligne créée en batch par le worker `index_document_chunks` après
    extraction + pre-cleaning + chunking + embedding. Cycle de vie lié au
    fichier parent : `ON DELETE CASCADE` sur une purge physique.
    """

    __tablename__ = "document_chunks"

    # BigInteger auto-increment — on prévoit des volumes élevés (1 Pro max
    # = 500 chunks × 50 docs × 10k Pros actifs = ~250M rows).
    id: Mapped[int] = mapped_column(
        BigInteger, autoincrement=True, primary_key=True, nullable=False
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)

    start_char_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)

    # pgvector colonne 1536 dim — même dim que D1 memories (alignement
    # OpenAI `text-embedding-3-small`).
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(
        String(64),
        server_default="text-embedding-3-small",
        default="text-embedding-3-small",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("file_id", "chunk_index", name="uq_document_chunks_file_idx"),
        CheckConstraint("chunk_index >= 0", name="ck_document_chunks_index_non_negative"),
        CheckConstraint(
            "token_count >= 1",
            name="ck_document_chunks_token_count_positive",
        ),
        CheckConstraint(
            "start_char_offset >= 0",
            name="ck_document_chunks_start_offset_non_negative",
        ),
        CheckConstraint(
            "end_char_offset >= start_char_offset",
            name="ck_document_chunks_offsets_ordered",
        ),
        CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name="ck_document_chunks_page_number_positive",
        ),
        # Index btree user_id et file_id — utile pour les requêtes RAG
        # de D5 (filter user + file_id pour scoper la recherche).
        Index("ix_document_chunks_user_id", "user_id"),
        Index("ix_document_chunks_file_id", "file_id"),
        # L'index HNSW vectoriel est en SQL brut dans la migration
        # (SQLAlchemy ORM ne le modélise pas).
    )
