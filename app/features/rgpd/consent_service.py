"""ConsentService — RGPD Article 7 (consentement explicite + révocabilité).

Session J1 — 2026-04-26.

API publique :
- `record(user, body, *, ip, user_agent, db)` : INSERT granted (idempotent
  sur `(user, type, document_version)`).
- `revoke(user, consent_type, *, ip, user_agent, db)` : INSERT revoked +
  UPDATE de l'ancien granted pour poser `revoked_at`.
- `is_granted(user, consent_type, db)` : bool, hot path appelé par
  d'autres features (ex: `/chat/stream` refuse si `ai_processing` non
  granted).
- `list_for_user(user, db)` : liste des consentements actifs (export RGPD
  + écran settings).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.auth_events import log_auth_event
from app.features.auth.models import User
from app.features.rgpd.models import ConsentLog
from app.features.rgpd.schemas import ConsentRecordRequest

log = structlog.get_logger(__name__)


class ConsentService:
    """Service stateless de gestion des consentements RGPD."""

    @staticmethod
    async def record(
        user: User,
        body: ConsentRecordRequest,
        *,
        ip: str | None,
        user_agent: str | None,
        db: AsyncSession,
    ) -> ConsentLog:
        """Enregistre un consentement granted.

        Idempotence : si un row identique
        `(user_id, consent_type, document_version, status='granted',
        revoked_at IS NULL)` existe déjà, retourne l'existant sans
        rien insérer.

        Si l'user avait consenti à une **autre version**, on revoke
        d'abord l'ancien (poser `revoked_at`), puis on insère le nouveau.
        Les anciens rows ne sont JAMAIS supprimés (preuve historique).
        """
        # 1. Lookup existant pour la même version
        result = await db.execute(
            select(ConsentLog).where(
                ConsentLog.user_id == user.id,
                ConsentLog.consent_type == body.consent_type,
                ConsentLog.document_version == body.document_version,
                ConsentLog.status == "granted",
                ConsentLog.revoked_at.is_(None),
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        # 2. Revoke l'ancien granted (autre version) si présent
        await db.execute(
            update(ConsentLog)
            .where(
                ConsentLog.user_id == user.id,
                ConsentLog.consent_type == body.consent_type,
                ConsentLog.status == "granted",
                ConsentLog.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )

        # 3. Insert nouveau granted
        row = ConsentLog(
            user_id=user.id,
            consent_type=body.consent_type,
            status="granted",
            document_version=body.document_version,
            document_hash=body.document_hash,
            ip_address=ip,
            user_agent=user_agent,
            source=body.source,
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
        await db.commit()

        # 4. Audit forensic
        await log_auth_event(
            db,
            event_type="consent_granted",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            metadata={
                "consent_type": body.consent_type,
                "document_version": body.document_version,
            },
        )

        log.info(
            "rgpd.consent.granted",
            user_id=str(user.id),
            consent_type=body.consent_type,
            document_version=body.document_version,
        )
        return row

    @staticmethod
    async def revoke(
        user: User,
        consent_type: str,
        *,
        ip: str | None,
        user_agent: str | None,
        db: AsyncSession,
    ) -> ConsentLog | None:
        """Révoque un consentement actif.

        Pattern : INSERT row `status='revoked'` (preuve horodatée de la
        révocation) + UPDATE l'ancien `granted` pour poser `revoked_at`.

        Si aucun consentement actif → retourne None (no-op idempotent).
        """
        result = await db.execute(
            select(ConsentLog).where(
                ConsentLog.user_id == user.id,
                ConsentLog.consent_type == consent_type,
                ConsentLog.status == "granted",
                ConsentLog.revoked_at.is_(None),
            )
        )
        active = result.scalar_one_or_none()
        if active is None:
            return None

        now = datetime.now(UTC)
        active.revoked_at = now

        revoked_row = ConsentLog(
            user_id=user.id,
            consent_type=consent_type,
            status="revoked",
            granted_at=active.granted_at,
            revoked_at=now,
            document_version=active.document_version,
            document_hash=active.document_hash,
            ip_address=ip,
            user_agent=user_agent,
            source="settings_screen",
        )
        db.add(revoked_row)
        await db.flush()
        await db.refresh(revoked_row)
        await db.commit()

        await log_auth_event(
            db,
            event_type="consent_revoked",
            user_id=user.id,
            ip=ip,
            user_agent=user_agent,
            metadata={
                "consent_type": consent_type,
                "document_version": active.document_version,
            },
        )

        log.info(
            "rgpd.consent.revoked",
            user_id=str(user.id),
            consent_type=consent_type,
        )
        return revoked_row

    @staticmethod
    async def is_granted(user_id: uuid.UUID, consent_type: str, db: AsyncSession) -> bool:
        """Hot path : `bool`, granted ET revoked_at IS NULL.

        Réutilisé par d'autres features (ex: `/chat/stream` peut refuser
        si `ai_processing` n'est pas granted).
        """
        result = await db.execute(
            select(ConsentLog.id)
            .where(
                ConsentLog.user_id == user_id,
                ConsentLog.consent_type == consent_type,
                ConsentLog.status == "granted",
                ConsentLog.revoked_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def list_for_user(user: User, db: AsyncSession) -> list[ConsentLog]:
        """Liste TOUS les consentements actifs (granted + revoked_at IS NULL).

        Utilisé par l'écran Settings + l'export RGPD (manifest.json).
        """
        result = await db.execute(
            select(ConsentLog)
            .where(
                ConsentLog.user_id == user.id,
                ConsentLog.status == "granted",
                ConsentLog.revoked_at.is_(None),
            )
            .order_by(ConsentLog.granted_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_history_for_user(user_id: uuid.UUID, db: AsyncSession) -> list[ConsentLog]:
        """Historique COMPLET (granted + revoked) — utilisé uniquement par
        l'export RGPD `consents.json` (preuve juridique de tout l'historique).
        """
        result = await db.execute(
            select(ConsentLog)
            .where(ConsentLog.user_id == user_id)
            .order_by(ConsentLog.granted_at.desc())
        )
        return list(result.scalars().all())
