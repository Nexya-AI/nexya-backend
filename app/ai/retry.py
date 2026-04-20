"""
NEXYA Couche IA — Politique de retry avec backoff exponentiel et jitter.

Deux types d'erreurs LLM qu'on gère :
- Transitoires (`retryable=True`) : 5xx, timeout, rate-limit 429 → on
  retente avec un délai croissant.
- Permanentes (`retryable=False`) : 401 (clé invalide), 400 (requête
  mal formée), content_filter → on ne retente JAMAIS, on remonte l'erreur.

Backoff exponentiel : délai = base × 2^attempt, plafonné à `max_delay`.
Jitter aléatoire ±25% pour désynchroniser les retries de 950 000 clients
qui taperaient tous en même temps (thundering herd).

Helper `stream_chat_with_retry` : wrappe un appel streaming. Subtilité :
on ne peut retenter QUE tant qu'aucun chunk n'est sorti — dès que le
premier chunk est livré, le stream est "engagé" et une erreur mid-stream
bubble up sans retry (sinon on enverrait du texte dupliqué au client).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog

from app.ai.providers import (
    ChatChunk,
    ChatCompletionRequest,
    ChatProvider,
    ProviderError,
    ProviderRateLimitError,
)

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# POLITIQUE
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Paramètres de retry. Valeurs par défaut raisonnables pour LLM streaming.

    - `max_attempts=3` : primaire + 2 retries. Au-delà, le circuit breaker
      prend le relais (si 3 échecs d'affilée → coupure temporaire).
    - `base_delay=0.5s` : premier retry quasi-immédiat, l'utilisateur ne
      perçoit pas la latence.
    - `max_delay=5s` : plafond pour que le total reste sous le timeout
      SSE client (< 8s avant que l'user clique "stop").
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 5.0
    jitter_ratio: float = 0.25

    def delay_for(self, attempt: int) -> float:
        """Calcule le délai avant la tentative `attempt` (0-indexed).

        attempt=0 → 0s (premier appel), attempt=1 → ~base, attempt=2 → ~base*2…
        Un `ProviderRateLimitError` peut transporter un `retry_after_seconds`
        explicite ; le caller l'applique avant d'entrer dans cette fonction.
        """
        if attempt <= 0:
            return 0.0
        raw = self.base_delay_seconds * (2 ** (attempt - 1))
        raw = min(raw, self.max_delay_seconds)
        jitter = raw * self.jitter_ratio * (random.random() * 2 - 1)
        return max(0.0, raw + jitter)


DEFAULT_POLICY = RetryPolicy()


# ═══════════════════════════════════════════════════════════════════
# STREAM CHAT AVEC RETRY
# ═══════════════════════════════════════════════════════════════════


async def stream_chat_with_retry(
    provider: ChatProvider,
    request: ChatCompletionRequest,
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
) -> AsyncIterator[ChatChunk]:
    """Appelle `provider.stream_chat` avec retry sur les erreurs `retryable`.

    Garanties :
    - Une erreur `retryable=False` → remontée immédiatement (pas de retry).
    - Une erreur AVANT le premier chunk → retry selon policy.
    - Une erreur APRÈS le premier chunk → remontée sans retry (stream engagé).
    - `asyncio.CancelledError` → propagée (l'utilisateur a annulé).
    """
    last_error: ProviderError | None = None

    for attempt in range(policy.max_attempts):
        delay = _compute_delay(policy, attempt, last_error)
        if delay > 0:
            log.info(
                "ai.retry.sleep",
                provider=provider.name,
                model=request.model,
                attempt=attempt,
                delay_seconds=round(delay, 3),
                trace_id=request.trace_id,
            )
            await asyncio.sleep(delay)

        try:
            generator = provider.stream_chat(request)
            first_chunk_received = False

            async for chunk in generator:
                first_chunk_received = True
                yield chunk

            if first_chunk_received:
                return
            # Générateur fermé sans rien yielder — bug provider, on traite comme erreur retryable
            raise _StreamEmptyError(provider=provider.name, model=request.model)

        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            if not exc.retryable:
                log.info(
                    "ai.retry.non_retryable",
                    provider=provider.name,
                    model=request.model,
                    error_type=type(exc).__name__,
                    trace_id=request.trace_id,
                )
                raise

            last_error = exc
            remaining = policy.max_attempts - attempt - 1
            log.warning(
                "ai.retry.attempt_failed",
                provider=provider.name,
                model=request.model,
                attempt=attempt,
                remaining=remaining,
                error_type=type(exc).__name__,
                status_code=exc.status_code,
                trace_id=request.trace_id,
            )
            if remaining <= 0:
                raise

    # Par sûreté, ne devrait pas être atteint
    if last_error is not None:
        raise last_error


# ═══════════════════════════════════════════════════════════════════
# INTERNES
# ═══════════════════════════════════════════════════════════════════


class _StreamEmptyError(ProviderError):
    """Le provider a terminé sans livrer un seul chunk. Traité comme retryable."""

    def __init__(self, *, provider: str, model: str | None) -> None:
        super().__init__(
            "Provider stream_chat returned no chunks.",
            provider=provider,
            model=model,
            retryable=True,
            status_code=502,
        )


def _compute_delay(
    policy: RetryPolicy,
    attempt: int,
    last_error: ProviderError | None,
) -> float:
    if isinstance(last_error, ProviderRateLimitError) and last_error.retry_after_seconds:
        return min(last_error.retry_after_seconds, policy.max_delay_seconds)
    return policy.delay_for(attempt)
