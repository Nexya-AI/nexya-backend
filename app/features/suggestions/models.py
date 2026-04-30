"""Modèle ORM `UserSuggestion` — Session N1.

Formulaire user → équipe NEXYA. FK `user_id ON DELETE SET NULL` pour
RGPD safety : si l'user demande la suppression de son compte, sa
suggestion reste anonyme dans la queue (utile roadmap produit + l'email
a déjà été envoyé à l'équipe au moment de la submit).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class UserSuggestion(Base, UUIDMixin):
    """Une suggestion (bug, feature, expert_domain, other) soumise par un user."""

    __tablename__ = "user_suggestions"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    suggestion_type: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    processing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "suggestion_type IN ('bug', 'feature', 'expert_domain', 'other')",
            name="ck_user_suggestions_type",
        ),
        CheckConstraint(
            "char_length(body) BETWEEN 1 AND 2000",
            name="ck_user_suggestions_body_length",
        ),
        CheckConstraint(
            "processing_status IN ('open', 'in_review', 'resolved', 'wontfix')",
            name="ck_user_suggestions_status",
        ),
        Index(
            "idx_user_suggestions_open_time",
            "created_at",
            postgresql_where=text("processing_status = 'open'"),
        ),
    )
