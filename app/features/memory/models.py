"""
Modèle ORM Memory — « faits durables » sur un utilisateur.

Aligné sur la migration `009_memories.py`. Utilisé comme **socle interne**
consommé par D2 (extraction post-conversation), D3 (injection system
prompt) et D5 (RAG endpoint).

Discipline :

- **Pas de relation `User.memories`** — anti-N+1 systématique aligné
  Conversation / Project / Library / UploadedFile.
- **Colonne `embedding` typée `Vector(1536)`** via
  `pgvector.sqlalchemy.Vector` — SQLAlchemy envoie/lit la liste de
  floats nativement, pas de sérialisation manuelle.
- **Dim figée v1** : changer nécessite une migration backfill coûteuse
  (Phase 12). La valeur `embedding_dim` est dupliquée sur la row pour
  traçabilité forensique (identifier les rows à backfill lors d'un
  changement de modèle).
- **`metadata_json JSONB`** : extension future (tags, importance
  pondérée, confidence score, lien vers la preuve textuelle…) sans
  migration DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class Memory(Base, UUIDMixin):
    """Mémoire IA utilisateur — un fait durable + son vecteur d'embedding.

    Exemples de contenus :
    - « Ivan est développeur Flutter »
    - « Ivan habite au Cameroun et travaille sur NEXYA »
    - « Ivan préfère les explications avec des analogies concrètes »
    - « Ivan est allergique aux arachides » (mémoire santé/user-critical)

    Un vecteur `embedding vector(1536)` stocke la représentation
    sémantique du `content`, produite par un `EmbeddingsProvider` au
    moment de l'`add`. Le search cosinus via l'index HNSW retourne les
    mémoires sémantiquement les plus proches d'une query utilisateur.
    """

    __tablename__ = "memories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    # pgvector colonne — SQLAlchemy délègue au type natif vector(1536).
    # `list[float]` côté Python, sérialisation transparente.
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(
        SmallInteger, server_default="1536", default=1536, nullable=False
    )

    source: Mapped[str] = mapped_column(
        String(16), server_default="manual", default="manual", nullable=False
    )
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
    )

    importance: Mapped[int] = mapped_column(
        SmallInteger, server_default="1", default=1, nullable=False
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "char_length(trim(content)) BETWEEN 1 AND 2000",
            name="ck_memories_content_length",
        ),
        CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_memories_sha256_length",
        ),
        CheckConstraint(
            "source IN ('manual','extracted','imported','system')",
            name="ck_memories_source",
        ),
        CheckConstraint(
            "importance BETWEEN 0 AND 10",
            name="ck_memories_importance_range",
        ),
        CheckConstraint(
            "embedding_dim > 0",
            name="ck_memories_dim_positive",
        ),
        # Index partiels — les 2 SQL-brut (UNIQUE partial + HNSW) sont
        # dans la migration (SQLAlchemy ORM ne les modélise pas).
        Index(
            "ix_memories_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_memories_user_source",
            "user_id",
            "source",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_memories_conv",
            "source_conversation_id",
            postgresql_where=text("deleted_at IS NULL AND source_conversation_id IS NOT NULL"),
        ),
    )
