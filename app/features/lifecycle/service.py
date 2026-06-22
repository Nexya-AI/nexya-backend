"""LifecycleEmailService — idempotence + gate consentement des emails lifecycle.

Pattern « claim » : avant d'envoyer un email lifecycle, on réserve atomiquement
un slot `(user_id, email_key)` via INSERT ON CONFLICT DO NOTHING. Si le slot est
déjà pris → on n'envoie pas (déjà envoyé). Si l'envoi échoue ensuite, on
`release` (DELETE) le slot pour qu'un prochain run réessaie.

Gate consentement : un email lifecycle (onboarding / digest / annonce) respecte
la préférence de catégorie du user. Si le canal est `none` (l'user s'est
désinscrit), on n'envoie pas. C'est la conformité RGPD/CAN-SPAM pour le
marketing/lifecycle (à la différence des emails transactionnels obligatoires).
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.lifecycle.models import LifecycleEmail
from app.features.notifications.preferences import NotificationPreferencesService

log = structlog.get_logger(__name__)


class LifecycleEmailService:
    """Idempotence + gate des emails lifecycle. Méthodes statiques."""

    @staticmethod
    async def claim(user_id: uuid.UUID, email_key: str, db: AsyncSession) -> bool:
        """Réserve atomiquement le slot `(user_id, email_key)`.

        Retourne True si le slot vient d'être créé (→ on peut envoyer),
        False s'il existait déjà (→ déjà envoyé, on skip). Idempotent via
        INSERT ON CONFLICT DO NOTHING RETURNING.
        """
        stmt = (
            pg_insert(LifecycleEmail)
            .values(user_id=user_id, email_key=email_key)
            .on_conflict_do_nothing(index_elements=["user_id", "email_key"])
            .returning(LifecycleEmail.id)
        )
        result = await db.execute(stmt)
        claimed = result.scalar_one_or_none() is not None
        await db.commit()
        return claimed

    @staticmethod
    async def release(user_id: uuid.UUID, email_key: str, db: AsyncSession) -> None:
        """Libère le slot (DELETE) — appelé si l'envoi a échoué, pour
        permettre un nouvel essai au prochain run du cron. Fail-safe."""
        try:
            await db.execute(
                delete(LifecycleEmail).where(
                    LifecycleEmail.user_id == user_id,
                    LifecycleEmail.email_key == email_key,
                )
            )
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lifecycle.release_failed",
                user_id=str(user_id),
                email_key=email_key,
                error=str(exc),
            )

    @staticmethod
    async def should_send_lifecycle(user: User, category: str, db: AsyncSession) -> bool:
        """True si l'user accepte de recevoir cet email lifecycle.

        Gate sur la préférence de catégorie : canal `none` = désinscrit → False.
        Les emails lifecycle sont email-first ; `push`/`email`/`both` → on envoie
        l'email. Fail-safe : sur erreur de lecture des prefs, on **n'envoie pas**
        (conservateur — mieux vaut rater un onboarding qu'emailer un désinscrit).
        """
        try:
            channel = await NotificationPreferencesService.get_channel_for_category(
                user.id, category, db
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lifecycle.prefs_lookup_failed",
                user_id=str(user.id),
                category=category,
                error=str(exc),
            )
            return False
        return channel != "none"
