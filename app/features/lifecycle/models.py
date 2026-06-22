"""Modèle ORM `LifecycleEmail` — journal idempotent des emails lifecycle.

Aligné migration `030_lifecycle_emails.py`. `created_at` (UUIDMixin) = date
d'envoi. Pas de relation inverse `User.lifecycle_emails` (anti-N+1, pattern NEXYA).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class LifecycleEmail(Base, UUIDMixin):
    """Trace l'envoi d'un email lifecycle à un user (idempotence).

    `email_key` : identifiant libre de l'email (ex. `onboarding_d1`,
    `reengagement_d7`, `digest_2026_W25`). UNIQUE par (user, key) → un même
    email n'est jamais envoyé deux fois.
    """

    __tablename__ = "lifecycle_emails"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email_key: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "email_key", name="uq_lifecycle_emails_user_key"),
        Index(
            "ix_lifecycle_emails_key_time",
            "email_key",
            text("created_at DESC"),
        ),
    )
