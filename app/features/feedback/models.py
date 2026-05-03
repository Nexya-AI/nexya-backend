"""Modèle ORM `MessageFeedback` — Session N1.

Trace les thumbs up/down posés par un user sur un message assistant.
UNIQUE composite `(user_id, message_id)` garantit qu'un même user ne peut
poser qu'un seul feedback par message — l'UPSERT atomique côté service
réutilise cet index pour l'idempotence.

Pas de relation `User.feedbacks` ni `Message.feedbacks` (anti-N+1
systématique aligné NEXYA).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class MessageFeedback(Base, UUIDMixin):
    """Un feedback (thumbs up/down) posé par un user sur un message assistant."""

    __tablename__ = "message_feedback"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # **D1.5-fix (2026-05-03)** — types DateTime explicites avec timezone=True
    # pour matcher la migration 018 qui crée les colonnes en
    # `TIMESTAMP WITH TIME ZONE`. Sans ce type explicite, SQLAlchemy
    # générait un cast `::TIMESTAMP WITHOUT TIME ZONE` dans l'UPSERT et
    # Postgres rejetait les valeurs `datetime(tzinfo=UTC)` Python →
    # 500 INTERNAL_ERROR sur tap thumbs-up frontend.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "rating IN ('like', 'dislike')",
            name="ck_message_feedback_rating",
        ),
        CheckConstraint(
            "comment IS NULL OR char_length(comment) <= 1000",
            name="ck_message_feedback_comment_length",
        ),
        UniqueConstraint("user_id", "message_id", name="uq_message_feedback_user_message"),
        Index(
            "idx_message_feedback_message_rating",
            "message_id",
            "rating",
        ),
    )
