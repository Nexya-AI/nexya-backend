"""ORM `HelpdeskEscalation` aligné migration 019_helpdesk."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.core.database.base import Base, UUIDMixin


class HelpdeskEscalation(Base, UUIDMixin):
    """Trace d'une escalation auto vers Crisp.

    Lifecycle : `created` → INSERT row avec `status='open'` →
    `CrispEscalator.escalate` tente l'API Crisp → si succès, UPDATE
    `crisp_conversation_id`. L'équipe support change `status` ensuite
    (in_progress/resolved/cancelled) via Crisp ou via un futur endpoint
    admin V2.
    """

    __tablename__ = "helpdesk_escalations"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    crisp_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "category IN ('payment','llm_unavailable','data_loss','rgpd','security')",
            name="ck_helpdesk_category",
        ),
        CheckConstraint(
            "severity IN ('low','medium','high','critical')",
            name="ck_helpdesk_severity",
        ),
        CheckConstraint(
            "status IN ('open','in_progress','resolved','cancelled')",
            name="ck_helpdesk_status",
        ),
        CheckConstraint(
            "(resolved_at IS NULL) OR (resolved_at >= created_at)",
            name="ck_helpdesk_resolved_after_created",
        ),
        Index(
            "ix_helpdesk_user_created",
            "user_id",
            "created_at",
            postgresql_where="deleted_at IS NULL",
        ),
    )
