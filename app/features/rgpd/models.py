"""Modèles ORM RGPD — `ConsentLog` + `DeletionRequest`.

Session J1 — Conformité RGPD Articles 7, 17 + AI Act EU Article 13.

Pas de relation `User.consent_logs` ni `User.deletion_requests` —
anti-pattern N+1 systématique aligné `Conversation`/`Project`/etc.
Le service charge directement via `select(ConsentLog).where(user_id=...)`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class ConsentLog(Base, UUIDMixin):
    """Trace **chaque évolution** de consentement utilisateur.

    Preuve juridique horodatée du consentement (Article 7 RGPD).

    Cycle de vie typique :
    - INSERT row `status='granted'` au premier consentement.
    - INSERT nouveau row `status='revoked'` à la révocation +
      UPDATE de l'ancien row pour poser `revoked_at=NOW()`.
    - Les anciens rows ne sont JAMAIS supprimés (preuve historique).

    Le couple `(document_version, document_hash)` figé au moment du
    consentement empêche NEXYA de modifier la ToS rétroactivement et
    prétendre que le user avait consenti à la nouvelle version.
    """

    __tablename__ = "consent_log"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    consent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "consent_type IN ('tos','privacy_policy','ai_processing',"
            "'ai_training_data','marketing_email','analytics','cookies')",
            name="ck_consent_log_type",
        ),
        CheckConstraint("status IN ('granted','revoked')", name="ck_consent_log_status"),
        CheckConstraint(
            "source IN ('register','settings_screen','api','cookies_banner','admin_grant')",
            name="ck_consent_log_source",
        ),
        CheckConstraint(
            "char_length(document_hash) = 64",
            name="ck_consent_log_hash_length",
        ),
        CheckConstraint(
            "(status = 'granted' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_consent_log_status_revoked_consistency",
        ),
        Index(
            "ix_consent_log_user_active",
            "user_id",
            "consent_type",
            postgresql_where=text("status = 'granted' AND revoked_at IS NULL"),
        ),
        Index("ix_consent_log_user_time", "user_id", "granted_at"),
    )


class DeletionRequest(Base, UUIDMixin):
    """Demande de suppression de compte avec workflow 2-step.

    Article 17 RGPD (droit à l'oubli) avec délai de grâce (30j par
    défaut, configurable). Le hard delete physique cascade SQL est
    exécuté par le worker arq `purge_deleted_accounts` quand
    `scheduled_purge_at` est passé.

    Idempotence stricte via index unique partiel sur `user_id WHERE
    status IN ('pending','processing')` — un user ne peut avoir qu'UNE
    demande active à la fois.
    """

    __tablename__ = "deletion_requests"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    scheduled_purge_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    purged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    purge_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','cancelled','processing','completed','failed')",
            name="ck_deletion_requests_status",
        ),
        CheckConstraint(
            "scheduled_purge_at >= requested_at",
            name="ck_deletion_requests_schedule_order",
        ),
    )
