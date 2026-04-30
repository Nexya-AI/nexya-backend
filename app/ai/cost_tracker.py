"""
NEXYA Couche IA — CostTracker DB (brique B3).

Persiste chaque appel LLM terminé dans deux tables :

1. **`ai_calls`** : détail forensic (insert simple).
2. **`usage_daily`** : agrégat par `(user_id, date_utc)` (UPSERT atomique).

Principes clés :

- **Fire-and-forget**. Le StreamHandler appelle `record_ai_call(...)` via
  `asyncio.create_task(...)` : le SSE ne bloque JAMAIS sur cette écriture.
  Si le DB est lent, le user a sa réponse et l'écriture se termine en
  arrière-plan. Si elle crash, on log warning et on passe — jamais
  d'exception propagée.

- **Session fraîche (`AsyncSessionLocal`)**. On ne réutilise PAS la
  session DB de la requête FastAPI — elle peut être rollback-ée par un
  post-processing du router chat (finalisation `Message`) ou être déjà
  fermée quand l'`asyncio.create_task` s'exécute. `AsyncSessionLocal()`
  ouvre une nouvelle connexion, indépendante.

- **UPSERT atomique**. Pattern `INSERT … ON CONFLICT (user_id, date_utc)
  DO UPDATE SET … RETURNING` : TOCTOU-safe, pas de pré-SELECT, deux
  streams simultanés du même user ne s'écrasent pas.

- **user_id nullable**. Les appels sans user (tests, batch, etc.) sont
  quand même stockés dans `ai_calls` avec `user_id=NULL`. Pour
  `usage_daily`, la PK `(user_id, date_utc)` accepte `user_id=NULL` en
  bucket "anonyme" — utile aussi pour les users post-RGPD.

- **Idempotence par session_id**. La colonne `ai_calls.session_id` est
  UNIQUE nullable. Si le `session_id` est déjà en base (ex. re-flush du
  SessionStore), on NE réécrit PAS — on log `cost_tracker.duplicate_session`
  et on continue. Évite les doubles comptabilisations si un flush est
  rejoué.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database.postgres import AsyncSessionLocal

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════════


class CostTracker:
    """Persiste les appels LLM dans `ai_calls` + `usage_daily`.

    Stateless (aucun champ interne) — le seul état est en DB.
    Instancié une fois au boot via `get_cost_tracker()` (runtime.py).
    """

    async def record_ai_call(
        self,
        *,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
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
        """Persiste un appel LLM. **Ne lève jamais** — fail-safe.

        Ouvre une session fraîche (`AsyncSessionLocal`), insère la ligne
        `ai_calls`, UPSERT la ligne `usage_daily`, commit.
        Sur erreur DB : log + return silencieux.
        """
        try:
            await self._persist(
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                expert_id=expert_id,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=_to_decimal(cost_usd),
                outcome=outcome,
                failure_code=failure_code,
                first_chunk_ms=first_chunk_ms,
                total_duration_ms=total_duration_ms,
                attempts=attempts,
                fallback_used=fallback_used,
                extra=extra,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Fail-safe : un échec de persistance ne doit JAMAIS remonter.
            log.warning(
                "cost_tracker.persist_failed",
                error=str(exc),
                provider=provider,
                model=model,
                outcome=outcome,
            )

    def record_ai_call_background(
        self,
        *,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
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
    ) -> asyncio.Task[None]:
        """Variante fire-and-forget : lance `record_ai_call` dans une task.

        Usage typique depuis le StreamHandler en fin de SSE :

            cost_tracker.record_ai_call_background(
                user_id=user_id, session_id=sid, ...
            )

        Le SSE ne bloque pas, l'écriture DB se fait en arrière-plan.
        La tâche retournée peut être ignorée ; les exceptions y sont
        de toute façon swallowed par `record_ai_call`.
        """
        coro = self.record_ai_call(
            user_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            expert_id=expert_id,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            outcome=outcome,
            failure_code=failure_code,
            first_chunk_ms=first_chunk_ms,
            total_duration_ms=total_duration_ms,
            attempts=attempts,
            fallback_used=fallback_used,
            extra=extra,
        )
        return asyncio.create_task(coro)

    # ─── Interne ─────────────────────────────────────────────────────

    async def _persist(
        self,
        *,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        trace_id: str | None,
        expert_id: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: Decimal,
        outcome: str,
        failure_code: str | None,
        first_chunk_ms: int | None,
        total_duration_ms: int | None,
        attempts: int,
        fallback_used: bool,
        extra: dict[str, Any] | None,
    ) -> None:
        today = datetime.now(UTC).date()

        async with AsyncSessionLocal() as db:
            # ── Insert ai_calls ───────────────────────────────────
            # Si session_id est déjà pris (flush du SessionStore
            # rejoué par exemple), on attrape l'IntegrityError UNIQUE
            # et on log — pas d'échec.
            try:
                await db.execute(
                    text(
                        """
                        INSERT INTO ai_calls (
                            user_id, session_id, trace_id, expert_id,
                            provider, model,
                            prompt_tokens, completion_tokens, total_tokens,
                            cost_usd, outcome, failure_code,
                            first_chunk_ms, total_duration_ms,
                            attempts, fallback_used, extra
                        )
                        VALUES (
                            :user_id, :session_id, :trace_id, :expert_id,
                            :provider, :model,
                            :prompt_tokens, :completion_tokens, :total_tokens,
                            :cost_usd, :outcome, :failure_code,
                            :first_chunk_ms, :total_duration_ms,
                            :attempts, :fallback_used, CAST(:extra AS JSONB)
                        )
                        """
                    ),
                    {
                        "user_id": user_id,
                        "session_id": session_id,
                        "trace_id": trace_id,
                        "expert_id": expert_id,
                        "provider": provider,
                        "model": model,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "cost_usd": cost_usd,
                        "outcome": outcome,
                        "failure_code": failure_code,
                        "first_chunk_ms": first_chunk_ms,
                        "total_duration_ms": total_duration_ms,
                        "attempts": attempts,
                        "fallback_used": fallback_used,
                        "extra": _jsonify(extra),
                    },
                )
            except IntegrityError:
                await db.rollback()
                log.info(
                    "cost_tracker.duplicate_session",
                    session_id=str(session_id) if session_id else None,
                )
                return

            # ── UPSERT usage_daily ────────────────────────────────
            # Pas d'incrément de cost/tokens si l'appel est failed —
            # on garde la trace dans ai_calls mais on ne facture pas
            # un appel qui n'a rien produit au user.
            if outcome in ("completed", "cancelled"):
                await db.execute(
                    text(
                        """
                        INSERT INTO usage_daily (
                            user_id, date_utc,
                            chat_calls, chat_tokens_in, chat_tokens_out,
                            image_calls, cost_usd,
                            created_at, updated_at
                        )
                        VALUES (
                            :user_id, :date_utc,
                            1, :prompt_tokens, :completion_tokens,
                            0, :cost_usd,
                            NOW(), NOW()
                        )
                        ON CONFLICT (user_id, date_utc) DO UPDATE SET
                            chat_calls = usage_daily.chat_calls + 1,
                            chat_tokens_in = usage_daily.chat_tokens_in + :prompt_tokens,
                            chat_tokens_out = usage_daily.chat_tokens_out + :completion_tokens,
                            cost_usd = usage_daily.cost_usd + :cost_usd,
                            updated_at = NOW()
                        """
                    ),
                    {
                        "user_id": user_id,
                        "date_utc": today,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "cost_usd": cost_usd,
                    },
                )

            await db.commit()

            log.info(
                "cost_tracker.recorded",
                user_id=str(user_id) if user_id else None,
                session_id=str(session_id) if session_id else None,
                provider=provider,
                model=model,
                expert_id=expert_id,
                outcome=outcome,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=float(cost_usd),
            )


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════


def _to_decimal(value: float | Decimal) -> Decimal:
    """Convertit float → Decimal via str pour éviter la perte de
    précision du binaire IEEE 754 (float 0.1 ≠ Decimal('0.1'))."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _jsonify(extra: dict[str, Any] | None) -> str | None:
    """Serialize dict → string JSON pour la colonne JSONB.

    `None` passe tel quel. Les bindings SQLAlchemy ne gèrent pas
    automatiquement le cast vers JSONB sur une chaîne texte sans le
    `CAST(... AS JSONB)` explicite dans la requête (fait plus haut).
    """
    if extra is None:
        return None
    import json

    return json.dumps(extra, default=str)
