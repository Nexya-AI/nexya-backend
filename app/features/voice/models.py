"""
Modèle ORM `VoiceTranscription` — Session E1 (Voice Pro-only).

Aligné sur la migration `012_voice_transcriptions.py`. Trace chaque
appel Whisper (ou futur faster-whisper) avec son `model`, `provider`,
`cost_usd`, permettant l'analyse portabilité a posteriori.

Discipline :
- Pas de relation `User.voice_transcriptions` (anti-N+1 systématique).
- `source_file_id` nullable : si l'audio est uploadé direct via
  `/voice/transcribe`, reste `NULL`. Si l'user a pré-uploadé via
  `/files/upload` puis appelé transcribe avec `upload_id`, trace le lien.
  `ON DELETE SET NULL` côté DB — une purge du fichier ne supprime pas la
  transcription (dont la valeur textuelle reste pertinente).
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
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class VoiceTranscription(Base, UUIDMixin):
    """Trace d'une transcription Whisper (ou futur faster-whisper).

    `cost_usd` permet l'agrégation `SUM(cost_usd) GROUP BY model` pour
    benchmarker le coût d'un provider vs un autre avant de switcher.
    """

    __tablename__ = "voice_transcriptions"

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

    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    transcribed_text: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(8))
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(8, 3), nullable=False)
    model: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6),
        server_default="0",
        default=Decimal("0"),
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_voice_sha256_length",
        ),
        CheckConstraint(
            "duration_seconds >= 0",
            name="ck_voice_duration_non_negative",
        ),
        CheckConstraint("cost_usd >= 0", name="ck_voice_cost_non_negative"),
        Index(
            "ix_voice_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_voice_source_file",
            "source_file_id",
            postgresql_where=text("deleted_at IS NULL AND source_file_id IS NOT NULL"),
        ),
    )
