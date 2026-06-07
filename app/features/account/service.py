"""C4.11 — `QuotasService` : agrège quotas user pour `GET /user/quotas`.

3 sources de données :
  1. **Docs PDF/DOCX ce mois** : SQL COUNT sur `library_items` filtré
     `source='generated' AND file_type IN ('pdf', 'docx')
     AND created_at >= DATE_TRUNC('month', NOW())`.
  2. **Voice minutes today** : Redis bucket `budget:user:{uid}:voice_minutes:{YYYY-MM-DD}`
     (déjà alimenté par `BudgetTracker.check_and_consume_voice_minutes`
     en E1). Pour Free : retourne None (pas d'accès Whisper backend,
     UX cachée côté Flutter — decision Ivan Q1=A).
  3. **Library storage cumulé** : SQL SUM(size_bytes) sur items actifs
     (réutilise `LibraryService._sum_storage_bytes` C4.11 Phase 2.3).

`reset_at` : 1er du mois suivant UTC minuit (helper local pur).

Pattern fail-safe absolu sur Redis (un blip Redis → voice_minutes_today=0
pas une 5xx) — l'endpoint quotas est read-only informatif, ne doit
JAMAIS bloquer l'ouverture du dashboard Account côté Flutter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database.redis import get_redis
from app.features.auth.models import User
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class UserQuotasSnapshot:
    """Snapshot agrégé retourné par `QuotasService.compute` (DTO interne).

    Le router le convertit en `UserQuotasResponse` Pydantic pour la réponse
    HTTP. Pattern aligné `LibraryItemWithVersions` C4.11 (DTO interne
    découplé du schéma exposé).
    """

    docs_generated_this_month: int
    docs_max_month: int
    voice_minutes_today: int | None  # None pour Free (pas d'accès)
    voice_minutes_max_day: int | None
    library_storage_bytes: int
    library_storage_max_bytes: int
    reset_at: datetime
    plan: str


# ══════════════════════════════════════════════════════════════
# Helpers dates
# ══════════════════════════════════════════════════════════════


def _today_utc_str() -> str:
    """Format `YYYY-MM-DD` pour la clé Redis voice_minutes (E1 pattern)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _next_month_utc_midnight() -> datetime:
    """C4.11 — Prochain 1er du mois UTC à minuit (reset compteurs mensuels).

    Pattern : si on est le 15 juin 2026 14h32 UTC → retourne
    1er juillet 2026 00h00 UTC. Si on est le 1er juin 2026 00h01 UTC →
    retourne 1er juillet 2026 00h00 UTC (le reset s'est déjà fait, on
    annonce le suivant).
    """
    now = datetime.now(UTC)
    # Si on est en décembre, le prochain mois est janvier de l'année suivante.
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, 0, 0, 0, tzinfo=UTC)
    return datetime(now.year, now.month + 1, 1, 0, 0, 0, tzinfo=UTC)


def _start_of_current_month_utc() -> datetime:
    """1er du mois courant UTC à minuit (borne basse pour COUNT docs)."""
    now = datetime.now(UTC)
    return datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════
# QuotasService
# ══════════════════════════════════════════════════════════════


class QuotasService:
    """C4.11 — Agrège les quotas user pour le dashboard Account."""

    @staticmethod
    async def compute(user: User, db: AsyncSession) -> UserQuotasSnapshot:
        """Calcule le snapshot complet des quotas pour l'user courant.

        3 queries en parallèle conceptuel (mais séquentielles V1 — la
        latence cumulée est négligeable ~5-15ms total) :
        - SQL COUNT docs ce mois (1 round-trip Postgres)
        - SQL SUM storage cumulé (1 round-trip Postgres, réutilise helper)
        - Redis GET voice_minutes today (1 round-trip Redis, fail-safe → 0)

        Caps lus depuis `settings` selon `user.is_pro`. Voice cap est
        `None` pour Free (UX UI cache la carte — decision Ivan Q1=A).
        """
        plan = "pro" if user.is_pro else "free"

        # 1. Docs PDF/DOCX générés ce mois UTC
        docs_count = await QuotasService._count_docs_this_month(user.id, db)
        docs_max = (
            settings.documents_quota_max_pro if user.is_pro else settings.documents_quota_max_free
        )

        # 2. Voice minutes today (Pro only — None pour Free, carte cachée)
        voice_minutes_today: int | None = None
        voice_minutes_max: int | None = None
        if user.is_pro:
            voice_minutes_today = await QuotasService._get_voice_minutes_today(user.id)
            voice_minutes_max = settings.voice_minutes_pro_per_day

        # 3. Library storage cumulé (réutilise helper LibraryService C4.11)
        storage_bytes = await LibraryService._sum_storage_bytes(user.id, db)
        storage_max = (
            settings.library_storage_max_bytes_pro
            if user.is_pro
            else settings.library_storage_max_bytes_free
        )

        return UserQuotasSnapshot(
            docs_generated_this_month=docs_count,
            docs_max_month=docs_max,
            voice_minutes_today=voice_minutes_today,
            voice_minutes_max_day=voice_minutes_max,
            library_storage_bytes=storage_bytes,
            library_storage_max_bytes=storage_max,
            reset_at=_next_month_utc_midnight(),
            plan=plan,
        )

    @staticmethod
    async def _count_docs_this_month(user_id: uuid.UUID, db: AsyncSession) -> int:
        """SQL COUNT docs PDF+DOCX générés ce mois UTC (scope user)."""
        month_start = _start_of_current_month_utc()
        stmt = select(func.count(LibraryItem.id)).where(
            LibraryItem.user_id == user_id,
            LibraryItem.source == "generated",
            LibraryItem.file_type.in_(["pdf", "docx"]),
            LibraryItem.created_at >= month_start,
            # Exclut les soft-deleted (consistant avec la définition
            # « docs actifs » du dashboard — un doc supprimé ne compte
            # plus dans le quota mensuel).
            LibraryItem.deleted_at.is_(None),
        )
        raw = (await db.execute(stmt)).scalar_one() or 0
        return int(raw)

    @staticmethod
    async def _get_voice_minutes_today(user_id: uuid.UUID) -> int:
        """Redis GET `budget:user:{uid}:voice_minutes:{YYYY-MM-DD}`.

        Fail-safe absolu : Redis down → return 0 (le dashboard reste
        consultable, l'user voit juste « 0 min today » au pire — moins
        critique qu'une 5xx qui casserait l'écran).
        """
        try:
            redis_client = get_redis()
            if redis_client is None:
                return 0
            key = f"budget:user:{user_id}:voice_minutes:{_today_utc_str()}"
            raw = await redis_client.get(key)
            if raw is None:
                return 0
            # Redis retourne bytes en mode async, decode + int
            if isinstance(raw, bytes):
                raw = raw.decode("ascii", errors="ignore")
            return int(raw)
        except Exception as exc:  # noqa: BLE001 fail-safe absolu
            log.warning(
                "quotas.voice_minutes.redis_failed",
                user_id=str(user_id),
                error_type=type(exc).__name__,
            )
            return 0


# Re-exports utilitaires pour les helpers de date (utilisés par tests)
__all__ = [
    "QuotasService",
    "UserQuotasSnapshot",
    "_next_month_utc_midnight",
    "_start_of_current_month_utc",
]
