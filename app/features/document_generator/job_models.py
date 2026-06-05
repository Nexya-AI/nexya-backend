"""Modèle ORM `DocumentJob` — génération asynchrone de documents (C4.12).

Aligné migration `028_document_jobs_and_documents_notif_category.py`.

Un `DocumentJob` trace l'état d'une génération de document lourd (> seuil
`documents_generator_async_threshold_chars`) qui a été déportée sur le worker
arq pour ne pas bloquer la requête HTTP. Cycle de vie :

    queued  → row créée par le router (`generate_or_enqueue`), job enqueué arq.
    processing → worker a pris le job, rendu WeasyPrint/docx en cours.
    done    → document généré + sauvé en Library, push FCM « doc prêt » envoyé.
    failed  → render/storage KO, push FCM d'échec envoyé, error_code tracé.

Sert à 3 usages :
1. Polling de secours côté Flutter (`GET /generate/document/jobs/{id}`) si le
   push FCM est manqué (réseau 2G/3G Africa, app killed, etc.).
2. Idempotence stricte du worker (double-livraison arq → skip si != 'queued').
3. Audit forensic + futur cron-recovery des jobs orphelins (status bloqué).

**Anti-tampering** : le contenu markdown source n'est PAS stocké dans
`params_json` — le worker re-fetch depuis `messages` via le couple
`(conversation_id, message_id)` (la source de vérité reste la DB, un user ne
peut pas modifier le contenu IA avant rendu).

Pas de relation inverse `User.document_jobs` (anti-N+1 systématique NEXYA).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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


class DocumentJob(Base, UUIDMixin):
    """Job de génération asynchrone d'un document (PDF ou DOCX)."""

    __tablename__ = "document_jobs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Contexte source — re-fetch par le worker pour récupérer le markdown.
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        server_default="queued",
        default="queued",
        nullable=False,
    )

    # Paramètres de rendu (alignés DocumentGenerateRequest).
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    template: Mapped[str] = mapped_column(String(32), nullable=False)
    # options (subject/level/date_iso/...) + title + remove_watermark.
    # JAMAIS le markdown source (anti-tampering — re-fetch en DB).
    params_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    # Résultat (peuplé quand status='done').
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Erreur (peuplée quand status='failed').
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'processing', 'done', 'failed')",
            name="ck_document_jobs_status",
        ),
        CheckConstraint(
            "format IN ('pdf', 'docx')",
            name="ck_document_jobs_format",
        ),
        # Liste active de l'user (polling + futur écran « mes générations »).
        Index(
            "ix_document_jobs_user_active",
            "user_id",
            text("created_at DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Cron-recovery futur : jobs orphelins bloqués en queued/processing.
        Index(
            "ix_document_jobs_pending",
            "created_at",
            postgresql_where=text("status IN ('queued', 'processing')"),
        ),
    )
