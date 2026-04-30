"""DeletionRequestService — workflow 2-step Article 17 RGPD.

Session J1 — 2026-04-26.

Pattern :
1. **Demande user** → `create_request()` :
   - Vérifie qu'aucune demande active n'existe (idempotence stricte).
   - Anonymise logiquement l'User (préserve A1 : email/username effacés,
     `is_active=False`, `deleted_at=NOW()`).
   - Capture l'email original AVANT anonymisation pour pouvoir envoyer
     la confirmation post-purge → stocké dans `purge_summary_json`.
   - Crée le DeletionRequest `status='pending'` +
     `scheduled_purge_at = NOW() + grace_period`.
   - Audit `account_delete_requested`.

2. **Rétractation user** (avant J+30) → `cancel_request()` :
   - Trouve la demande pending.
   - Status → `'cancelled'`.
   - Restaure `is_active=True`, `deleted_at=NULL`.
   - Audit `account_delete_cancelled`.
   - Note : email/username restent anonymisés (rétractation = fenêtre
     courte, on ne reconstitue pas l'identité d'origine).

3. **Hard delete** → cron `workers/rgpd_tasks.purge_deleted_accounts`
   (cf. L6) qui exécute le DELETE FROM users (cascade SQL) +
   suppression des blobs MinIO.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import NexYaException
from app.features.auth.auth_events import log_auth_event
from app.features.auth.models import User
from app.features.rgpd.models import DeletionRequest

log = structlog.get_logger(__name__)


class DeletionRequestAlreadyExistsException(NexYaException):
    """Une demande de suppression est déjà en cours pour cet user."""

    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            code="DELETION_REQUEST_ALREADY_EXISTS",
            message=(
                "Une demande de suppression est déjà en cours. "
                "Annulez-la avant d'en créer une nouvelle."
            ),
        )


class NoActiveDeletionRequestException(NexYaException):
    """Aucune demande active à annuler."""

    def __init__(self) -> None:
        super().__init__(
            status_code=404,
            code="NO_ACTIVE_DELETION_REQUEST",
            message="Aucune demande de suppression active à annuler.",
        )


class DeletionRequestService:
    """Workflow 2-step de suppression de compte (Article 17 RGPD)."""

    @staticmethod
    async def _get_active_request(user_id: uuid.UUID, db: AsyncSession) -> DeletionRequest | None:
        result = await db.execute(
            select(DeletionRequest).where(
                DeletionRequest.user_id == user_id,
                DeletionRequest.status.in_(("pending", "processing")),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def create_request(
        user: User,
        *,
        reason: str | None,
        ip: str | None,
        user_agent: str | None,
        db: AsyncSession,
    ) -> DeletionRequest:
        """Crée une demande de suppression (workflow 2-step).

        Idempotence : si demande active existe → 409. Pas de no-op
        silencieux pour ne pas masquer une potentielle attaque
        (compte compromis qui spamme delete-request).
        """
        existing = await DeletionRequestService._get_active_request(user.id, db)
        if existing is not None:
            raise DeletionRequestAlreadyExistsException()

        now = datetime.now(UTC)
        scheduled = now + timedelta(days=settings.rgpd_deletion_grace_period_days)

        # Capture l'email original AVANT anonymisation pour pouvoir
        # envoyer la confirmation post-purge.
        original_email = user.email

        # Anonymisation logique (A1 — préservé).
        user.email = f"deleted_{uuid.uuid4().hex[:12]}@nexya.ai"
        user.username = None
        user.display_name = "Utilisateur supprime"
        user.avatar_url = None
        user.bio = None
        user.is_active = False
        user.deleted_at = now
        user.updated_at = now

        request = DeletionRequest(
            user_id=user.id,
            requested_at=now,
            scheduled_purge_at=scheduled,
            status="pending",
            reason=reason,
            purge_summary_json={
                # Stocké AVANT le hard delete cascade pour pouvoir
                # envoyer le mail de confirmation depuis le worker.
                "email_for_confirmation": original_email,
            },
            ip_address=ip,
            user_agent=user_agent,
        )
        db.add(request)
        await db.flush()
        await db.refresh(request)
        await db.commit()

        await log_auth_event(
            db,
            event_type="account_delete_requested",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            metadata={
                "scheduled_purge_at": scheduled.isoformat(),
                "request_id": str(request.id),
            },
        )

        log.info(
            "rgpd.deletion.requested",
            user_id=str(user.id),
            request_id=str(request.id),
            scheduled_purge_at=scheduled.isoformat(),
        )
        return request

    @staticmethod
    async def cancel_request(
        user: User,
        *,
        ip: str | None,
        user_agent: str | None,
        db: AsyncSession,
    ) -> DeletionRequest:
        """Annule une demande active (rétractation user avant J+grace)."""
        existing = await DeletionRequestService._get_active_request(user.id, db)
        if existing is None or existing.status != "pending":
            raise NoActiveDeletionRequestException()

        existing.status = "cancelled"
        existing.updated_at = datetime.now(UTC)

        # Réactivation user (email reste anonymisé — la rétractation
        # ne reconstitue pas l'identité d'origine, l'user devra
        # contacter le support pour ça).
        user.is_active = True
        user.deleted_at = None
        user.updated_at = datetime.now(UTC)

        await db.flush()
        await db.refresh(existing)
        await db.commit()

        await log_auth_event(
            db,
            event_type="account_delete_cancelled",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            metadata={"request_id": str(existing.id)},
        )

        log.info(
            "rgpd.deletion.cancelled",
            user_id=str(user.id),
            request_id=str(existing.id),
        )
        return existing

    @staticmethod
    async def fetch_due_for_purge(
        db: AsyncSession, *, batch_size: int = 50
    ) -> list[DeletionRequest]:
        """Charge les demandes pending dont scheduled_purge_at est passé.

        Utilisé par le cron worker (cf. L6) avec SELECT FOR UPDATE
        SKIP LOCKED côté worker.
        """
        now = datetime.now(UTC)
        result = await db.execute(
            select(DeletionRequest)
            .where(
                DeletionRequest.status == "pending",
                DeletionRequest.scheduled_purge_at <= now,
            )
            .order_by(DeletionRequest.scheduled_purge_at.asc())
            .limit(batch_size)
        )
        return list(result.scalars().all())

    @staticmethod
    async def mark_processing(request: DeletionRequest, db: AsyncSession) -> None:
        request.status = "processing"
        request.updated_at = datetime.now(UTC)
        await db.flush()

    @staticmethod
    async def mark_completed(
        request: DeletionRequest,
        *,
        purge_summary: dict,
        db: AsyncSession,
    ) -> None:
        request.status = "completed"
        request.purged_at = datetime.now(UTC)
        request.updated_at = datetime.now(UTC)
        # Merge le summary existant (qui contient email_for_confirmation)
        # avec le nouveau (tables_purged, blobs_deleted, duration_ms).
        existing = request.purge_summary_json or {}
        merged = {**existing, **purge_summary}
        request.purge_summary_json = merged
        await db.flush()

    @staticmethod
    async def mark_failed(request: DeletionRequest, *, error: str, db: AsyncSession) -> None:
        request.status = "failed"
        request.updated_at = datetime.now(UTC)
        existing = request.purge_summary_json or {}
        existing["last_error"] = error
        request.purge_summary_json = existing
        await db.flush()
