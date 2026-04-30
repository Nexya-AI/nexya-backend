"""
Modèle ORM `ExpertCorpusChunk` — corpus RAG global par expert (G1).

Aligné sur la migration `016_expert_corpus_chunks.py`. Consommé par D3-miroir
`build_expert_corpus_context` (injection system prompt avant les system
prompts expert métier). Pas de relation utilisateur — le corpus est un
bien commun de la plateforme.

Discipline :

- **Dim 768 figée au DDL** (Gemini `text-embedding-004`). Switch vers
  1536 (OpenAI) = migration + re-ingestion complète (voir docstring
  migration 016 pour la procédure).
- **`embedding_model` dupliqué sur la row** : traçabilité forensique
  lors d'un changement de modèle — identifier précisément les rows à
  backfill.
- **`metadata_json JSONB`** : extension future sans migration DDL
  (difficulty score, attribution source, license, date d'ingestion, etc.).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


class ExpertCorpusChunk(Base):
    """Chunk d'un corpus expert — texte atomique + vecteur d'embedding.

    Exemples de contenus :
    - `[FR] J'apprends le français depuis trois mois\n[ES] Aprendo francés
      desde hace tres meses` (expert `language`, source=`tatoeba`,
      language_pair=`fra-spa`).
    - `[FR] Recette du ndolè camerounais : ...` (expert `cooking`,
      source=`nexya-cookbook-2026`, language_pair=None).

    Le retrieval filtre systématiquement par `expert_slug` (pas de
    fuite cross-expert), puis applique le cosinus pgvector sur
    `embedding <=> :q_vec` limité top-K.
    """

    __tablename__ = "expert_corpus_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    expert_slug: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    embedding: Mapped[list[float]] = mapped_column(Vector(768), nullable=False)
    embedding_model: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default="gemini-embedding-001",
        default="gemini-embedding-001",
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    language_pair: Mapped[str | None] = mapped_column(String(16))

    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_expert_corpus_sha256_length",
        ),
        CheckConstraint(
            "char_length(content) >= 1",
            name="ck_expert_corpus_content_non_empty",
        ),
        UniqueConstraint(
            "expert_slug",
            "content_sha256",
            name="uq_expert_corpus_slug_sha",
        ),
        Index("ix_expert_corpus_slug", "expert_slug"),
        Index(
            "ix_expert_corpus_slug_lang",
            "expert_slug",
            "language_pair",
        ),
        # Index HNSW déclaré en SQL brut dans la migration.
    )
