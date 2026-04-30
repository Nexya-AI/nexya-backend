"""
NEXYA Couche IA — BudgetTracker (filet de sécurité anti-runaway).

Rôle : poser des plafonds ABSOLUS sur la consommation IA, en complément
des quotas métier (Free 50/jour, Pro 1000/jour). C'est le "fusible" qui
évite qu'un bug ou un abus fasse exploser la facture LLM.

Quatre couches de limitation orthogonales :
1. **Chat par user et par jour** — max ~200 messages/jour (même Pro).
2. **Images par user et par jour** — max ~50 images/jour (plus cher).
3. **Burst par IP et par minute** — max 20 req/min.
4. **Plafond global par modèle et par jour** — cassure d'urgence si un
   modèle Pro (Gemini 2.5 Pro, GPT-4o) s'envole.

Algorithme : Redis INCR + EXPIRE sur des clés datées — identique au
`rate_limiter` existant, pas de Lua, pas de module Redis requis.

Dégradation si Redis est down : **fail-open** (log error) — ne jamais
bloquer un utilisateur parce que Redis déraille. Le CostTracker (à
venir) aura sa propre persistance PostgreSQL, donc aucune donnée
utilisateur n'est perdue.

Différence avec `core/security/rate_limiter.py` :
- `rate_limiter` = limitation par IP des endpoints d'auth non authentifiés.
- `BudgetTracker` = limitation IA par user authentifié + plafonds globaux.
Les deux cohabitent ; ils ne s'écrasent pas.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
import structlog

from app.core.database.redis import get_redis
from app.core.errors.exceptions import RateLimitExceededException

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES — PLAFONDS PAR DÉFAUT
# ═══════════════════════════════════════════════════════════════════
#
# Valeurs calibrées pour que :
# - Un utilisateur légitime (même power-user Pro) ne les atteigne JAMAIS.
# - Un bug de boucle infinie côté client soit coupé net en quelques minutes.
# - Le coût worst-case reste soutenable (~0.40 USD/user/jour au pire).
#
# Ces constantes sont la base ; peuvent être override à la construction.
# ═══════════════════════════════════════════════════════════════════

DEFAULT_USER_CHAT_PER_DAY = 200  # messages IA / user / jour
DEFAULT_USER_IMAGE_PER_DAY = 50  # images / user / jour
DEFAULT_USER_EMBEDDINGS_PER_DAY = 10_000  # appels embed() / user / jour (D1)
DEFAULT_USER_VOICE_MINUTES_PER_DAY = 120  # minutes Whisper / user / jour (E1 Pro only)
DEFAULT_USER_TTS_CHARS_PER_DAY = 50_000  # chars TTS / user / jour (E1 Pro only)
DEFAULT_USER_VISION_IMAGES_PER_DAY = 50  # images / user / jour (E2 — Free=3, Pro=50)
DEFAULT_IP_BURST_PER_MINUTE = 20  # requêtes / IP / minute (toutes features)
DEFAULT_GLOBAL_MODEL_PER_DAY = 100_000  # appels / modèle / jour (plafond global)

_PREFIX = "budget:"
_DAY_TTL_SECONDS = 60 * 60 * 24 * 2  # 48h — marge pour fuseaux horaires
_MINUTE_TTL_SECONDS = 90  # 90s — marge pour les secondes intermédiaires


# ═══════════════════════════════════════════════════════════════════
# TYPES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Photo de la consommation d'un user à l'instant T, sans consommer."""

    user_id: str
    chat_used_today: int
    chat_limit: int
    image_used_today: int
    image_limit: int

    @property
    def chat_remaining(self) -> int:
        return max(0, self.chat_limit - self.chat_used_today)

    @property
    def image_remaining(self) -> int:
        return max(0, self.image_limit - self.image_used_today)


# ═══════════════════════════════════════════════════════════════════
# BUDGETTRACKER
# ═══════════════════════════════════════════════════════════════════


class BudgetTracker:
    """Vérifie et incrémente atomiquement les compteurs de consommation IA.

    Méthodes `check_and_consume_*` :
    - Si la consommation projetée (current + cost) dépasse le plafond, lèvent
      `RateLimitExceededException` AVANT d'incrémenter (pas de surconsommation).
    - Sinon, incrémentent atomiquement et retournent.

    Toutes les opérations échouent en mode fail-open en cas d'erreur Redis.
    """

    def __init__(
        self,
        *,
        user_chat_per_day: int = DEFAULT_USER_CHAT_PER_DAY,
        user_image_per_day: int = DEFAULT_USER_IMAGE_PER_DAY,
        user_embeddings_per_day: int = DEFAULT_USER_EMBEDDINGS_PER_DAY,
        user_voice_minutes_per_day: int = DEFAULT_USER_VOICE_MINUTES_PER_DAY,
        user_tts_chars_per_day: int = DEFAULT_USER_TTS_CHARS_PER_DAY,
        user_vision_images_per_day: int = DEFAULT_USER_VISION_IMAGES_PER_DAY,
        ip_burst_per_minute: int = DEFAULT_IP_BURST_PER_MINUTE,
        global_model_per_day: int = DEFAULT_GLOBAL_MODEL_PER_DAY,
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        self.user_chat_per_day = user_chat_per_day
        self.user_image_per_day = user_image_per_day
        self.user_embeddings_per_day = user_embeddings_per_day
        self.user_voice_minutes_per_day = user_voice_minutes_per_day
        self.user_tts_chars_per_day = user_tts_chars_per_day
        self.user_vision_images_per_day = user_vision_images_per_day
        self.ip_burst_per_minute = ip_burst_per_minute
        self.global_model_per_day = global_model_per_day
        self._redis = redis_client  # Si None, get_redis() au moment de l'appel

    # ─── API publique ────────────────────────────────────────────────

    async def check_and_consume_chat(self, user_id: str, *, cost: int = 1) -> int:
        """Incrémente le compteur chat/jour de `cost` et renvoie le nouveau total.

        Lève `RateLimitExceededException` si le plafond serait franchi.
        """
        return await self._check_and_incr(
            key=self._user_day_key(user_id, kind="chat"),
            cost=cost,
            limit=self.user_chat_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="user_chat_day",
            metadata={"user_id": user_id},
            reset_at=_next_midnight_utc(),
        )

    async def check_and_consume_image(self, user_id: str, *, cost: int = 1) -> int:
        """Idem pour la génération d'images. `cost` = nombre d'images demandées."""
        return await self._check_and_incr(
            key=self._user_day_key(user_id, kind="image"),
            cost=cost,
            limit=self.user_image_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="user_image_day",
            metadata={"user_id": user_id, "count": cost},
            reset_at=_next_midnight_utc(),
        )

    async def check_and_consume_embeddings(self, user_id: str, *, cost: int = 1) -> int:
        """Compteur journalier d'appels `embed()` côté user (Session D1).

        Chaque `MemoryStore.add` et chaque `MemoryStore.search` consomme
        1 crédit (coût minuscule de l'API OpenAI `text-embedding-3-small`,
        mais utile pour couper un script abusif avant de brûler la facture).
        """
        return await self._check_and_incr(
            key=self._user_day_key(user_id, kind="embeddings"),
            cost=cost,
            limit=self.user_embeddings_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="user_embeddings_day",
            metadata={"user_id": user_id, "count": cost},
            reset_at=_next_midnight_utc(),
        )

    async def check_and_consume_voice_minutes(self, user_id: str, *, minutes: int = 1) -> int:
        """Compteur journalier de minutes Whisper consommées (Session E1).

        `minutes` est un entier (on arrondit à la minute supérieure dans
        l'appelant). Utilisé **avant** l'appel API Whisper pour bloquer
        un Pro qui dépasserait son quota journalier avant de brûler les
        crédits OpenAI.

        Si l'estimation pré-appel était trop haute (ex: audio plus court
        que prévu), l'appelant peut rembourser via `refund_voice_minutes`.
        """
        if minutes < 1:
            return 0
        return await self._check_and_incr(
            key=self._user_day_key(user_id, kind="voice_minutes"),
            cost=minutes,
            limit=self.user_voice_minutes_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="user_voice_minutes_day",
            metadata={"user_id": user_id, "minutes": minutes},
            reset_at=_next_midnight_utc(),
        )

    async def refund_voice_minutes(self, user_id: str, *, minutes: int) -> None:
        """Rembourse un excédent d'estimation de minutes voice.

        Utilisé quand la durée estimée avant appel Whisper était plus
        haute que la durée réelle retournée par l'API. Ne raise jamais
        (fail-safe Redis error).
        """
        if minutes <= 0:
            return
        redis = self._get_redis()
        key = self._user_day_key(user_id, kind="voice_minutes")
        try:
            new_value = await redis.decrby(key, minutes)
            if new_value < 0:
                await redis.set(key, 0)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ai.budget.refund_error",
                scope="voice_minutes",
                user_id=user_id,
                error=str(exc),
            )

    async def check_and_consume_tts_chars(self, user_id: str, *, chars: int) -> int:
        """Compteur journalier de caractères TTS consommés (Session E1).

        Unité = caractères, pas minutes — parce que la facturation
        OpenAI TTS est per-character. 50k chars/jour ≈ 1h d'audio
        synthétisé à 10 mots/seconde.
        """
        if chars < 1:
            return 0
        return await self._check_and_incr(
            key=self._user_day_key(user_id, kind="tts_chars"),
            cost=chars,
            limit=self.user_tts_chars_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="user_tts_chars_day",
            metadata={"user_id": user_id, "chars": chars},
            reset_at=_next_midnight_utc(),
        )

    async def check_and_consume_vision_images(self, user_id: str, *, images: int = 1) -> int:
        """Compteur journalier d'images vision analysées (Session E2).

        Comptage par image (pas tokens) pour rester prévisible côté user.
        Une image Full HD = ~400-800 tokens Gemini ; on plafonne par
        nombre d'images car c'est l'UX visible pour l'user (« j'ai
        analysé 3 images aujourd'hui »).
        """
        if images < 1:
            return 0
        return await self._check_and_incr(
            key=self._user_day_key(user_id, kind="vision_images"),
            cost=images,
            limit=self.user_vision_images_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="user_vision_images_day",
            metadata={"user_id": user_id, "images": images},
            reset_at=_next_midnight_utc(),
        )

    async def refund_vision_images(self, user_id: str, *, images: int) -> None:
        """Rembourse N images (usage : provider down, on rembourse le
        consommé pré-appel). Fail-safe."""
        if images <= 0:
            return
        redis = self._get_redis()
        key = self._user_day_key(user_id, kind="vision_images")
        try:
            new_value = await redis.decrby(key, images)
            if new_value < 0:
                await redis.set(key, 0)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ai.budget.refund_error",
                scope="vision_images",
                user_id=user_id,
                error=str(exc),
            )

    async def check_and_consume_ip_burst(self, ip: str) -> int:
        """Incrémente le compteur IP/minute. À appeler avant l'auth pour couper
        tôt les floods."""
        return await self._check_and_incr(
            key=self._ip_minute_key(ip),
            cost=1,
            limit=self.ip_burst_per_minute,
            ttl_seconds=_MINUTE_TTL_SECONDS,
            scope="ip_burst_minute",
            metadata={"ip": ip},
            reset_at=None,
        )

    async def check_and_consume_model(self, model: str, *, cost: int = 1) -> int:
        """Plafond global par modèle/jour — cassure d'urgence, pas un quota user."""
        return await self._check_and_incr(
            key=self._model_day_key(model),
            cost=cost,
            limit=self.global_model_per_day,
            ttl_seconds=_DAY_TTL_SECONDS,
            scope="global_model_day",
            metadata={"model": model},
            reset_at=_next_midnight_utc(),
        )

    async def snapshot(self, user_id: str) -> BudgetSnapshot:
        """Lit les compteurs actuels sans les muter. Utile pour `/me/usage`."""
        redis = self._get_redis()
        chat_key = self._user_day_key(user_id, kind="chat")
        image_key = self._user_day_key(user_id, kind="image")
        try:
            chat_raw, image_raw = await redis.mget(chat_key, image_key)
        except Exception as exc:  # noqa: BLE001
            log.error("ai.budget.snapshot_error", user_id=user_id, error=str(exc))
            chat_raw, image_raw = None, None
        return BudgetSnapshot(
            user_id=user_id,
            chat_used_today=_as_int(chat_raw),
            chat_limit=self.user_chat_per_day,
            image_used_today=_as_int(image_raw),
            image_limit=self.user_image_per_day,
        )

    # ─── Internes ────────────────────────────────────────────────────

    def _get_redis(self) -> aioredis.Redis:
        return self._redis if self._redis is not None else get_redis()

    async def _check_and_incr(
        self,
        *,
        key: str,
        cost: int,
        limit: int,
        ttl_seconds: int,
        scope: str,
        metadata: dict[str, object],
        reset_at: datetime | None,
    ) -> int:
        """Pattern atomique :
        1. INCRBY cost → nouvelle valeur.
        2. Si = cost → c'est le premier incr de la fenêtre → poser EXPIRE.
        3. Si > limit → DECRBY cost (rollback) + exception.

        Pourquoi INCRBY avant check : parce que INCR est atomique et un
        check-then-incr introduirait une race condition. Le rollback sur
        dépassement est acceptable (Redis vit avec ce pattern depuis
        toujours, et c'est linéarisable).
        """
        if cost < 1:
            return 0

        redis = self._get_redis()
        try:
            new_value = await redis.incrby(key, cost)
            if new_value == cost:
                # Premier incr de la fenêtre → poser le TTL
                await redis.expire(key, ttl_seconds)
            if new_value > limit:
                # Dépassement — rollback atomique (INCRBY négatif)
                await redis.decrby(key, cost)
                log.warning(
                    "ai.budget.exceeded",
                    scope=scope,
                    current=new_value - cost,
                    attempted_cost=cost,
                    limit=limit,
                    **metadata,
                )
                raise RateLimitExceededException(reset_at=reset_at)
            return int(new_value)
        except RateLimitExceededException:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open
            log.error(
                "ai.budget.redis_error",
                scope=scope,
                error=str(exc),
                error_type=type(exc).__name__,
                **metadata,
            )
            return 0

    # ─── Helpers clés ────────────────────────────────────────────────

    @staticmethod
    def _user_day_key(user_id: str, *, kind: str) -> str:
        return f"{_PREFIX}user:{user_id}:{kind}:{_today_utc()}"

    @staticmethod
    def _ip_minute_key(ip: str) -> str:
        return f"{_PREFIX}ip:{ip}:{_this_minute_utc()}"

    @staticmethod
    def _model_day_key(model: str) -> str:
        return f"{_PREFIX}model:{model}:{_today_utc()}"


# ═══════════════════════════════════════════════════════════════════
# HELPERS DATE — UTC partout (pas de surprise fuseaux)
# ═══════════════════════════════════════════════════════════════════


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _this_minute_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")


def _next_midnight_utc() -> datetime:
    now = datetime.now(UTC)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_int(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_tracker: BudgetTracker | None = None


def get_budget_tracker() -> BudgetTracker:
    """Instance partagée du tracker. Construite au premier appel."""
    global _tracker
    if _tracker is None:
        _tracker = BudgetTracker()
    return _tracker
