"""
NEXYA Couche IA — SessionStore Redis (brique B3).

Rôle : **filet de sécurité** au-dessus du CostTracker fire-and-forget.

Le StreamHandler écrit deux fois la trace d'un appel LLM terminé :

1. **Fast path** — `CostTracker.record_ai_call_background(...)` lance une
   `asyncio.create_task` qui insère directement dans `ai_calls` +
   UPSERT `usage_daily`. Côté "chemin heureux", cette écriture suffit.

2. **Slow path** — `SessionStore.record(session_id, payload)` stocke le
   même payload dans Redis sous la clé `ai:session:{session_id}` avec
   TTL 24 h. Un cron arq `flush_ai_sessions` toutes les 10 min scanne
   les clés et tente un INSERT `ai_calls` pour chaque session absente
   en DB. Grâce à `ai_calls.session_id UNIQUE`, l'INSERT est idempotent :
   si la ligne a déjà été écrite par le fast path, on l'ignore ; sinon
   on récupère la ligne perdue.

Cas couverts par le filet :

- Le worker uvicorn est tué (SIGKILL, OOM) pendant l'exécution de la
  task fire-and-forget du CostTracker.
- La DB est momentanément indisponible au moment de la task (l'exception
  est swallowed par le fail-safe du CostTracker → la ligne est perdue).
- Redis est OK mais Postgres était en maintenance pendant la fenêtre de
  fin de stream.

Format du payload Redis (JSON) :
- mêmes champs que les kwargs de `CostTracker.record_ai_call`.
- `stored_at` ISO-8601 : quand l'entrée a été posée (pour prioriser les
  plus anciennes dans le flush).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.database.redis import get_redis

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════

SESSION_KEY_PREFIX = "ai:session:"
SESSION_TTL_SECONDS = 24 * 3600  # 24 h

# SCAN par paquets — évite de charger des millions de clés en mémoire
# si la DB Postgres est down depuis plusieurs jours.
SCAN_BATCH_SIZE = 200


# ═══════════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════════


class SessionStore:
    """Tampon Redis des appels LLM récents.

    Stateless (pas d'état interne hors du client Redis). Instancié une
    fois au boot via `get_session_store()` (runtime.py).
    """

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis

    def _client(self) -> Redis:
        # Lazy : le pool Redis peut ne pas être prêt au moment où on
        # instancie le SessionStore (ordre d'import au démarrage).
        return self._redis or get_redis()

    # ─── Clés ────────────────────────────────────────────────────────

    @staticmethod
    def _key(session_id: uuid.UUID | str) -> str:
        return f"{SESSION_KEY_PREFIX}{session_id}"

    # ─── Écriture ────────────────────────────────────────────────────

    async def record(
        self,
        *,
        session_id: uuid.UUID | str,
        user_id: uuid.UUID | None,
        trace_id: str | None,
        expert_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float | Decimal,
        outcome: str,
        failure_code: str | None = None,
        first_chunk_ms: int | None = None,
        total_duration_ms: int | None = None,
        attempts: int = 1,
        fallback_used: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Stocke l'entrée Redis. **Fail-safe** : jamais de raise.

        Si Redis est indisponible, on log et on passe — le fast path
        CostTracker reste la voie principale, le SessionStore n'est qu'un
        filet, sa panne ne doit pas bloquer le stream.
        """
        payload = {
            "session_id": str(session_id),
            "user_id": str(user_id) if user_id else None,
            "trace_id": trace_id,
            "expert_id": expert_id,
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": str(cost_usd),  # Decimal → str pour JSON
            "outcome": outcome,
            "failure_code": failure_code,
            "first_chunk_ms": first_chunk_ms,
            "total_duration_ms": total_duration_ms,
            "attempts": attempts,
            "fallback_used": fallback_used,
            "extra": extra,
            "stored_at": datetime.now(UTC).isoformat(),
        }
        try:
            await self._client().set(
                self._key(session_id),
                json.dumps(payload, ensure_ascii=False, default=str),
                ex=SESSION_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — fail-safe
            log.warning(
                "session_store.record_failed",
                session_id=str(session_id),
                error=str(exc),
            )

    # ─── Lecture / flush ─────────────────────────────────────────────

    async def scan_pending(self, *, batch_size: int = SCAN_BATCH_SIZE) -> list[dict[str, Any]]:
        """Retourne toutes les entrées présentes dans le tampon.

        Utilise `SCAN` (non bloquant, itératif) plutôt que `KEYS` (bloque
        Redis et O(N) sur la totalité du keyspace). Parse les payloads
        JSON ; ignore silencieusement les entrées corrompues (log warning).
        """
        entries: list[dict[str, Any]] = []
        client = self._client()
        try:
            async for key in client.scan_iter(match=f"{SESSION_KEY_PREFIX}*", count=batch_size):
                try:
                    raw = await client.get(key)
                    if raw is None:
                        continue
                    entries.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError) as exc:
                    log.warning(
                        "session_store.corrupt_entry",
                        key=key,
                        error=str(exc),
                    )
                    # On supprime l'entrée corrompue pour ne pas la
                    # re-tenter à chaque flush.
                    try:
                        await client.delete(key)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001 — fail-safe
            log.error("session_store.scan_failed", error=str(exc))
            return []
        return entries

    async def delete(self, session_id: uuid.UUID | str) -> None:
        """Supprime l'entrée Redis (après flush DB réussi ou doublon)."""
        try:
            await self._client().delete(self._key(session_id))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "session_store.delete_failed",
                session_id=str(session_id),
                error=str(exc),
            )

    async def get(self, session_id: uuid.UUID | str) -> dict[str, Any] | None:
        """Lit une entrée unique — utile pour les tests et le debug."""
        try:
            raw = await self._client().get(self._key(session_id))
            if raw is None:
                return None
            return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "session_store.get_failed",
                session_id=str(session_id),
                error=str(exc),
            )
            return None


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_SESSION_STORE: SessionStore | None = None


def get_session_store() -> SessionStore:
    """Singleton process-wide du SessionStore."""
    global _SESSION_STORE
    if _SESSION_STORE is None:
        _SESSION_STORE = SessionStore()
    return _SESSION_STORE


def reset_session_store_for_tests() -> None:
    global _SESSION_STORE
    _SESSION_STORE = None
