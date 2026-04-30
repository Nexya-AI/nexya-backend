"""
Modèle ORM Library — `LibraryItem`.

Schéma SQL aligné sur la migration `007_library.py` et sur le modèle
Flutter `MediaModel` (types `image`/`video`/`gif`/`audio`/`document`
+ `file_type` pour les documents).

Discipline :

- **Pas de relation `User.library_items`** — même principe anti-N+1 que
  pour `conversations` et `projects`. Les listings passent par
  `LibraryService.list_for_user`.

- **Pas de relation eager `Conversation` / `Message`** sur
  `source_conversation_id` / `source_message_id` — on ne veut JAMAIS
  qu'un affichage de conversation déclenche un chargement de toute la
  biblio pré-cached.

- **`tags` comme `ARRAY(String)`** : array natif Postgres, pas une table
  de jointure. Le trafic est très asymétrique (quelques dizaines de tags
  max par item, lecture >> écriture) et l'index GIN sur l'array permet
  `WHERE 'cuisine' = ANY(tags)` en O(log N).

- **`metadata_json` en `JSONB`** : extension future (watermark C2PA,
  EXIF, seeds de génération) sans migration DDL. Indexable par JSON path
  si besoin.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class LibraryItem(Base, UUIDMixin):
    """Item de bibliothèque utilisateur — métadonnée + pointeur MinIO.

    Le binaire vit sur MinIO/S3 à l'adresse `storage_key`. Le backend
    ne sert JAMAIS le binaire par un stream applicatif — il fournit
    une presigned URL signée côté routeur / service que le client
    récupère directement sur MinIO.
    """

    __tablename__ = "library_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    file_type: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    aspect_ratio: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))

    source: Mapped[str] = mapped_column(
        String(16),
        server_default="uploaded",
        default="uploaded",
        nullable=False,
    )
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt: Mapped[str | None] = mapped_column(Text)

    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
    )

    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "type IN ('image', 'video', 'gif', 'audio', 'document', 'text')",
            name="ck_library_items_type",
        ),
        CheckConstraint(
            "file_type IS NULL OR file_type IN ('pdf', 'docx', 'xlsx', 'pptx', 'other')",
            name="ck_library_items_file_type",
        ),
        CheckConstraint(
            "char_length(trim(title)) BETWEEN 1 AND 200",
            name="ck_library_items_title_length",
        ),
        CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000",
            name="ck_library_items_description_length",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_library_items_size_non_negative"),
        CheckConstraint(
            "width_px IS NULL OR width_px > 0",
            name="ck_library_items_width_positive",
        ),
        CheckConstraint(
            "height_px IS NULL OR height_px > 0",
            name="ck_library_items_height_positive",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_library_items_duration_non_negative",
        ),
        CheckConstraint(
            "source IN ('generated', 'uploaded', 'imported', 'shared')",
            name="ck_library_items_source",
        ),
        CheckConstraint(
            "prompt IS NULL OR char_length(prompt) <= 4000",
            name="ck_library_items_prompt_length",
        ),
        CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_library_items_sha256_length",
        ),
        Index(
            "idx_library_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_library_user_type",
            "user_id",
            "type",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "idx_library_user_conversation",
            "user_id",
            "source_conversation_id",
            postgresql_where=text("deleted_at IS NULL AND source_conversation_id IS NOT NULL"),
        ),
    )
