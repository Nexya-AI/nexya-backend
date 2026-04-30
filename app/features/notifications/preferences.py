"""
NotificationPreferencesService — gestion des préférences user par catégorie × canal.

Pattern :
- Les defaults sont en dur en Python (`_DEFAULT_CHANNELS`) — changer les
  defaults ne nécessite pas de migration DB.
- `get_for_user` retourne toujours les 5 catégories (injecte les defaults
  pour celles sans row).
- `set_for_user` fait un UPSERT atomique via
  `pg_insert(...).on_conflict_do_update(index_elements=['user_id','category'])`.
- `_get_channel_for_category` est le hot-path consommé par le Dispatcher —
  une seule requête DB, retour default si absent.

Aucune relation inverse `User.notification_preferences` (anti-N+1 NEXYA).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.notifications.models import NotificationPreference

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Constantes
# ═══════════════════════════════════════════════════════════════════


CATEGORIES: tuple[str, ...] = (
    "tasks",
    "payments",
    "security",
    "digest",
    "product",
)

CHANNELS: frozenset[str] = frozenset({"push", "email", "both", "none"})

# Defaults par catégorie — appliqués si l'user n'a pas posé de row.
# Rationale :
# - `tasks=push`     : une tâche qui s'exécute = notif temps-réel, push.
# - `payments=email` : traçabilité + conformité (facture, reçu, RGPD).
# - `security=email` : trace écrite obligatoire pour audit forensic.
# - `digest=email`   : volume modéré, lecture asynchrone, email.
# - `product=email`  : annonces produit, volume faible, pas bloquant.
_DEFAULT_CHANNELS: dict[str, str] = {
    "tasks": "push",
    "payments": "email",
    "security": "email",
    "digest": "email",
    "product": "email",
}


@dataclass(frozen=True, slots=True)
class PreferenceEntry:
    """DTO interne renvoyé au service/router (pas un ORM)."""

    category: str
    channel: str


# ═══════════════════════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════════════════════


class NotificationPreferencesService:
    """CRUD préférences avec defaults injectés en mémoire."""

    @staticmethod
    async def get_for_user(user_id: uuid.UUID, db: AsyncSession) -> list[PreferenceEntry]:
        """Retourne les 5 catégories, avec defaults pour celles sans row."""
        result = await db.execute(
            select(
                NotificationPreference.category,
                NotificationPreference.channel,
            ).where(NotificationPreference.user_id == user_id)
        )
        rows = {row.category: row.channel for row in result.all()}
        return [
            PreferenceEntry(
                category=cat,
                channel=rows.get(cat, _DEFAULT_CHANNELS[cat]),
            )
            for cat in CATEGORIES
        ]

    @staticmethod
    async def set_for_user(
        user_id: uuid.UUID,
        updates: list[PreferenceEntry],
        db: AsyncSession,
    ) -> list[PreferenceEntry]:
        """UPSERT atomique de chaque ligne (user_id, category).

        Valide que les catégories et canaux sont dans les whitelists.
        Retourne l'état complet post-UPSERT (toujours 5 catégories).
        """
        now = datetime.now(tz=UTC)
        for update in updates:
            if update.category not in CATEGORIES:
                raise ValueError(f"Catégorie inconnue : {update.category!r}.")
            if update.channel not in CHANNELS:
                raise ValueError(f"Canal inconnu : {update.channel!r}.")

            stmt = pg_insert(NotificationPreference).values(
                user_id=user_id,
                category=update.category,
                channel=update.channel,
                created_at=now,
                updated_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_id", "category"],
                set_={
                    "channel": stmt.excluded.channel,
                    "updated_at": now,
                },
            )
            await db.execute(stmt)

        await db.commit()
        log.info(
            "notifications.prefs.updated",
            user_id=str(user_id),
            categories=[u.category for u in updates],
        )
        return await NotificationPreferencesService.get_for_user(user_id, db)

    @staticmethod
    async def set_category_none(user_id: uuid.UUID, category: str, db: AsyncSession) -> None:
        """Shortcut pour l'unsubscribe one-click : pose channel='none'.

        Idempotent : appelable plusieurs fois sans erreur.
        """
        if category not in CATEGORIES:
            raise ValueError(f"Catégorie inconnue : {category!r}.")
        now = datetime.now(tz=UTC)
        stmt = pg_insert(NotificationPreference).values(
            user_id=user_id,
            category=category,
            channel="none",
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "category"],
            set_={"channel": "none", "updated_at": now},
        )
        await db.execute(stmt)
        await db.commit()
        log.info(
            "notifications.prefs.unsubscribed",
            user_id=str(user_id),
            category=category,
        )

    @staticmethod
    async def get_channel_for_category(user_id: uuid.UUID, category: str, db: AsyncSession) -> str:
        """Hot-path dispatcher — une seule requête DB, fallback default."""
        if category not in CATEGORIES:
            return _DEFAULT_CHANNELS.get(category, "none")
        result = await db.execute(
            select(NotificationPreference.channel).where(
                NotificationPreference.user_id == user_id,
                NotificationPreference.category == category,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row
        return _DEFAULT_CHANNELS[category]


def default_channel_for(category: str) -> str:
    """Helper public pour les tests / dispatcher."""
    return _DEFAULT_CHANNELS.get(category, "none")
