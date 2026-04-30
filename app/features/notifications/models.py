"""
Modèles ORM Notifications — `Notification` + `NotificationPreference` (F3).

Aligné migration `015_notifications.py`. Pas de relation inverse
`User.notifications` (anti-N+1 systématique, pattern NEXYA).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class NotificationPreference(Base):
    """Préférence de canal par catégorie pour un user.

    Composite PK `(user_id, category)`. L'absence de row pour un couple
    signifie « utiliser le default Python côté service ».
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), primary_key=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "category IN ('tasks','payments','security','digest','product')",
            name="ck_notification_prefs_category",
        ),
        CheckConstraint(
            "channel IN ('push','email','both','none')",
            name="ck_notification_prefs_channel",
        ),
        Index(
            "ix_notification_prefs_user",
            "user_id",
            postgresql_where=text("channel != 'none'"),
        ),
    )


class Notification(Base, UUIDMixin):
    """Trace une notification envoyée (ou skippée) à un utilisateur.

    Sert à 3 usages :
    1. Timeline in-app (`GET /notifications`).
    2. Diagnostic client / audit forensic.
    3. Futurs dashboards admin (volumes par catégorie).
    """

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    channel_used: Mapped[str] = mapped_column(String(16), nullable=False)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    push_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    email_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    attempts_push: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    attempts_email: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "category IN ('tasks','payments','security','digest','product')",
            name="ck_notifications_category",
        ),
        CheckConstraint(
            "channel_used IN ('push','email','both','skipped')",
            name="ck_notifications_channel_used",
        ),
        CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name="ck_notifications_title_len",
        ),
        CheckConstraint(
            "attempts_push >= 0 AND attempts_email >= 0",
            name="ck_notifications_attempts_non_neg",
        ),
        CheckConstraint(
            "source_kind IN ('scheduled_task','payment','security','digest','product','manual')",
            name="ck_notifications_source_kind",
        ),
        Index(
            "ix_notifications_user_active",
            "user_id",
            text("sent_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_notifications_user_unread",
            "user_id",
            text("sent_at DESC"),
            postgresql_where=text("read_at IS NULL AND deleted_at IS NULL"),
        ),
        Index(
            "ix_notifications_category_time",
            "category",
            text("sent_at DESC"),
        ),
    )
