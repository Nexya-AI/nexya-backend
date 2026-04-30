"""
Worker arq — tâches de la Couche IA.

Pour l'instant, une seule tâche : `flush_ai_sessions` (cron toutes les
10 minutes) qui matérialise en DB les entrées du SessionStore Redis
qui n'auraient pas été persistées par le fast path CostTracker.

Voir `app/ai/engine/session_store.py` pour la logique complète du
filet de sécurité.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.ai.engine.session_store import get_session_store
from app.core.database.postgres import AsyncSessionLocal

log = structlog.get_logger(__name__)


# Outcomes valides — alignés sur la CHECK constraint de `ai_calls`.
_VALID_OUTCOMES = frozenset({"completed", "cancelled", "failed"})


async def flush_ai_sessions(ctx: dict[str, Any]) -> dict[str, int]:
    """Parcourt le SessionStore et tente un INSERT `ai_calls` par entrée.

    Stratégie :

    - `INSERT ... ON CONFLICT (session_id) DO NOTHING RETURNING id` : la
      contrainte UNIQUE sur `ai_calls.session_id` rend l'opération
      idempotente. Si le fast path (CostTracker) a déjà écrit la ligne,
      `RETURNING` est vide → on sait qu'on peut supprimer l'entrée Redis.
    - Si `RETURNING` renvoie un id, on a récupéré une ligne perdue →
      on supprime aussi l'entrée Redis.
    - UPSERT `usage_daily` uniquement si l'outcome est `completed` ou
      `cancelled` (même règle que le CostTracker fast path).
    - En cas d'erreur DB/Redis, on laisse l'entrée en place — elle sera
      retentée au prochain tick (le TTL 24 h absorbe plusieurs échecs).

    Retour : `{scanned, inserted, skipped_duplicate, errors}` pour
    alimenter les logs et éventuellement des métriques Prometheus.
    """
    store = get_session_store()
    entries = await store.scan_pending()
    if not entries:
        return {"scanned": 0, "inserted": 0, "skipped_duplicate": 0, "errors": 0}

    stats = {
        "scanned": len(entries),
        "inserted": 0,
        "skipped_duplicate": 0,
        "errors": 0,
    }

    for entry in entries:
        session_id_str = entry.get("session_id")
        if not session_id_str:
            stats["errors"] += 1
            continue

        try:
            inserted = await _persist_entry(entry)
        except Exception as exc:  # noqa: BLE001 — flush résiste
            log.warning(
                "ai_tasks.flush.persist_failed",
                session_id=session_id_str,
                error=str(exc),
            )
            stats["errors"] += 1
            continue

        # Insert OK OU doublon : dans les deux cas l'entrée Redis a fini
        # son rôle (la ligne DB existe), on la nettoie.
        await store.delete(session_id_str)
        if inserted:
            stats["inserted"] += 1
        else:
            stats["skipped_duplicate"] += 1

    log.info("ai_tasks.flush.completed", **stats)
    return stats


async def _persist_entry(entry: dict[str, Any]) -> bool:
    """INSERT idempotent d'une entrée SessionStore dans `ai_calls`.

    Retourne `True` si la ligne a été insérée (récupérée du filet),
    `False` si elle existait déjà (le fast path avait tenu).

    Lève sur erreur SQL irrécupérable — le caller décide de laisser
    l'entrée Redis pour le prochain tick.
    """
    session_id = _parse_uuid(entry.get("session_id"))
    if session_id is None:
        raise ValueError(f"session_id manquant ou invalide : {entry.get('session_id')}")

    user_id = _parse_uuid(entry.get("user_id"))
    outcome = entry.get("outcome") or "failed"
    if outcome not in _VALID_OUTCOMES:
        outcome = "failed"

    prompt_tokens = int(entry.get("prompt_tokens") or 0)
    completion_tokens = int(entry.get("completion_tokens") or 0)
    total_tokens = int(entry.get("total_tokens") or 0)
    cost_usd = _parse_decimal(entry.get("cost_usd"))

    today = datetime.now(UTC).date()

    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                text(
                    """
                    INSERT INTO ai_calls (
                        user_id, session_id, trace_id, expert_id,
                        provider, model,
                        prompt_tokens, completion_tokens, total_tokens,
                        cost_usd, outcome, failure_code,
                        first_chunk_ms, total_duration_ms,
                        attempts, fallback_used
                    )
                    VALUES (
                        :user_id, :session_id, :trace_id, :expert_id,
                        :provider, :model,
                        :prompt_tokens, :completion_tokens, :total_tokens,
                        :cost_usd, :outcome, :failure_code,
                        :first_chunk_ms, :total_duration_ms,
                        :attempts, :fallback_used
                    )
                    ON CONFLICT (session_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "session_id": session_id,
                    "trace_id": entry.get("trace_id"),
                    "expert_id": entry.get("expert_id") or "general",
                    "provider": entry.get("provider") or "unknown",
                    "model": entry.get("model") or "unknown",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "cost_usd": cost_usd,
                    "outcome": outcome,
                    "failure_code": entry.get("failure_code"),
                    "first_chunk_ms": entry.get("first_chunk_ms"),
                    "total_duration_ms": entry.get("total_duration_ms"),
                    "attempts": int(entry.get("attempts") or 1),
                    "fallback_used": bool(entry.get("fallback_used")),
                },
            )
            inserted_id = result.scalar_one_or_none()

            # UPSERT usage_daily uniquement si la ligne vient d'être insérée
            # ET que l'outcome est billable (completed/cancelled). Si la ligne
            # existait déjà, le CostTracker fast path a déjà fait l'UPSERT.
            if inserted_id is not None and outcome in ("completed", "cancelled"):
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
            return inserted_id is not None

        except SQLAlchemyError:
            await db.rollback()
            raise


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
