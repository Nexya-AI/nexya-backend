"""
Modèle ORM `VisionAnalysis` — Session E2.

Aligné migration `013_vision_analyses.py`. Trace chaque analyse image
avec `model`, `provider`, `cost_usd` pour benchmark portabilité.

Discipline :
- Pas de relation `User.vision_analyses` (anti-N+1 systématique).
- `source_file_id` / `source_library_id` nullables avec ON DELETE SET NULL —
  si l'user uploade d'abord via `/files/upload` ou via `POST /library`,
  on trace le lien. Si base64 direct, les deux restent NULL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class VisionAnalysis(Base, UUIDMixin):
    """Une analyse d'image par un LLM multimodal.

    Dédup par `(user_id, image_sha256, prompt_sha256)` UNIQUE partielle —
    ré-analyser le même couple (image, prompt) retourne la ligne existante.
    """

    __tablename__ = "vision_analyses"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("library_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    image_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    analysis_text: Mapped[str] = mapped_column(Text, nullable=False)

    model: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)

    tokens_input: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    tokens_output: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6),
        server_default="0",
        default=Decimal("0"),
        nullable=False,
    )

    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)

    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "char_length(image_sha256) = 64",
            name="ck_vision_image_sha256_length",
        ),
        CheckConstraint(
            "char_length(prompt_sha256) = 64",
            name="ck_vision_prompt_sha256_length",
        ),
        CheckConstraint("cost_usd >= 0", name="ck_vision_cost_non_negative"),
        CheckConstraint(
            "tokens_input >= 0 AND tokens_output >= 0",
            name="ck_vision_tokens_non_negative",
        ),
        Index(
            "ix_vision_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_vision_source_file",
            "source_file_id",
            postgresql_where=text("deleted_at IS NULL AND source_file_id IS NOT NULL"),
        ),
    )
