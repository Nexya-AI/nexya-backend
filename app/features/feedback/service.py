"""FeedbackService — UPSERT atomique des thumbs up/down chat (Session N1).

API publique :
- `record_feedback(user, message_id, body, db)` : UPSERT atomique via
  `pg_insert.on_conflict_do_update`. Si l'user a déjà posé un feedback
  sur ce message, met à jour rating + comment + updated_at. Sinon INSERT.
  Retourne la row finale.
- `delete_feedback(user, message_id, db)` : DELETE idempotent, 204 même
  si pas de row (anti-énumération).
- `_get_owned_message(message_id, user_id, db)` : 404 IDOR-safe via JOIN
  strict avec `conversations` (réutilise pattern `ChatService` /
  `ReportService`).

Pas de nouvelle métrique Prometheus en N1 — log structlog suffit V1.
N3 évals pourront ajouter `nexya_chat_feedback_total` dédié si besoin
agrégat temps-réel.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.auth.models import User
from app.features.chat.models import Conversation, Message
from app.features.feedback.models import MessageFeedback
from app.features.feedback.schemas import FeedbackCreate

log = structlog.get_logger(__name__)


class FeedbackService:
    """Service stateless de gestion des feedbacks chat."""

    @staticmethod
    async def _get_owned_message(
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Message:
        """Charge un message dont la conversation appartient à l'user.

        Owner-check via JOIN en une seule requête. Mismatch user →
        `ResourceNotFoundException` 404 (anti-énumération), jamais 403.
        Soft-delete sur conv ou message → 404 aussi.
        """
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.deleted_at.is_(None),
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        result = await db.execute(stmt)
        message = result.scalar_one_or_none()
        if message is None:
            raise ResourceNotFoundException("Message")
        return message

    @staticmethod
    async def record_feedback(
        user: User,
        message_id: uuid.UUID,
        body: FeedbackCreate,
        db: AsyncSession,
    ) -> MessageFeedback:
        """UPSERT atomique du feedback.

        Idempotence stricte au niveau DB via UNIQUE (user_id,
        message_id) + `on_conflict_do_update`. Pas de race TOCTOU
        possible entre 2 clics thumbs simultanés.
        """
        # 1. Owner check (404 si pas propriétaire de la conv)
        await FeedbackService._get_owned_message(message_id, user.id, db)

        # 2. UPSERT atomique
        now = datetime.now(UTC)
        stmt = (
            pg_insert(MessageFeedback)
            .values(
                user_id=user.id,
                message_id=message_id,
                rating=body.rating,
                comment=body.comment,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "message_id"],
                set_={
                    "rating": body.rating,
                    "comment": body.comment,
                    "updated_at": now,
                },
            )
            .returning(MessageFeedback)
        )
        result = await db.execute(stmt)
        row = result.scalar_one()
        await db.commit()

        log.info(
            "chat.feedback.recorded",
            user_id=str(user.id),
            message_id=str(message_id),
            rating=body.rating,
            has_comment=body.comment is not None,
        )
        return row

    @staticmethod
    async def delete_feedback(
        user: User,
        message_id: uuid.UUID,
        db: AsyncSession,
    ) -> None:
        """Supprime le feedback s'il existe — idempotent.

        Pas de 404 si la row n'existe pas : anti-énumération (un user
        ne peut pas distinguer « j'avais un feedback » vs « j'en avais
        pas » pour un message d'un autre user en testant DELETE).
        """
        from sqlalchemy import delete

        stmt = delete(MessageFeedback).where(
            MessageFeedback.user_id == user.id,
            MessageFeedback.message_id == message_id,
        )
        await db.execute(stmt)
        await db.commit()
        log.info(
            "chat.feedback.deleted",
            user_id=str(user.id),
            message_id=str(message_id),
        )

    @staticmethod
    async def get_for_message(
        user: User,
        message_id: uuid.UUID,
        db: AsyncSession,
    ) -> MessageFeedback | None:
        """Lecture optionnelle V1 — utile V2 pour rehydrater le bouton
        thumbs côté Flutter au reload conversation.
        """
        stmt = select(MessageFeedback).where(
            MessageFeedback.user_id == user.id,
            MessageFeedback.message_id == message_id,
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
