"""
NEXYA Couche IA — Streaming SSE robuste.

Orchestration de bout en bout d'un appel chat :

    Router → Chain → (pour chaque lien) CircuitBreaker → Retry → Provider
         ↓
    SSE événements (chunk / keepalive / done / error)
         ↓
    Client Flutter

Features critiques (CLAUDE.md section 6) :
- **Heartbeat 15s** — `: keepalive` toutes les 15s pour éviter la coupure
  des proxies 2G/3G après inactivité TCP.
- **Annulation coopérative** — deux voies :
  (a) FastAPI `Request.is_disconnected()` pour détecter que le mobile
      a fermé sa connexion HTTP (lock screen, switch d'app, perte réseau).
  (b) Clé Redis `chat:cancel:{session_id}` pour permettre un `POST /chat/stop`
      depuis le client, qui pose la clé et coupe le stream côté serveur.
- **Fallback chain** — si le provider primaire échoue (5xx/timeout après
  retry, circuit ouvert), on passe au fallback. Aucune interruption côté
  client : il voit simplement un stream "hiccup" de quelques secondes.
- **Épuisement de la chaîne** — si tous les providers tombent, on émet un
  événement SSE `error` avec code `LLM_UNAVAILABLE` puis `done` — pas
  d'exception HTTP 500 en plein stream (le frontend ne saurait pas la
  lire, le stream est déjà en cours).

Ce module ne fait PAS :
- Pas de modération (Brique 3 le fait avant d'entrer ici).
- Pas de comptage coût (Brique 7 accumulera usage).
- Pas de persistance messages (QueryEngine final s'en chargera).
Il est focalisé sur la qualité du transport SSE.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from fastapi import Request

from app.ai.circuit_breaker import (
    CircuitOpenError,
    CircuitBreakerRegistry,
    get_breaker_registry,
)
from app.ai.providers import (
    ChatChunk,
    ChatCompletionRequest,
    ChatMessage,
    FinishReason,
    ProviderError,
)
from app.ai.observability import StreamMetrics
from app.ai.retry import RetryPolicy, DEFAULT_POLICY, stream_chat_with_retry
from app.ai.router import ChatResolution, LlmRouter
from app.core.database.redis import get_redis

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════

HEARTBEAT_SECONDS = 15.0
CANCEL_CHECK_INTERVAL_SECONDS = 1.0
DISCONNECT_CHECK_INTERVAL_SECONDS = 2.0
CANCEL_KEY_PREFIX = "chat:cancel:"
CANCEL_KEY_TTL_SECONDS = 300  # 5 min — largement assez pour un chat long


# ═══════════════════════════════════════════════════════════════════
# SSE — helpers bas niveau
# ═══════════════════════════════════════════════════════════════════


def _sse(event: str, data: dict | str) -> str:
    """Formate un événement SSE. Retour : `event: ... \\n data: ... \\n\\n`."""
    if isinstance(data, dict):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    else:
        payload = data
    return f"event: {event}\ndata: {payload}\n\n"


def _keepalive_comment() -> str:
    """Commentaire SSE (ligne commençant par `:`) — ne déclenche aucun
    handler côté EventSource, mais relance le TCP keepalive."""
    return ": keepalive\n\n"


# ═══════════════════════════════════════════════════════════════════
# REDIS — annulation coopérative
# ═══════════════════════════════════════════════════════════════════


def _cancel_key(session_id: str) -> str:
    return f"{CANCEL_KEY_PREFIX}{session_id}"


async def mark_cancelled(session_id: str) -> None:
    """Pose la clé Redis qui signale au stream de s'arrêter.
    Appelée par `POST /chat/stop`.
    """
    if not session_id:
        return
    redis = get_redis()
    try:
        await redis.set(_cancel_key(session_id), "1", ex=CANCEL_KEY_TTL_SECONDS)
        log.info("ai.stream.cancel_marked", session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        log.error("ai.stream.cancel_mark_error", session_id=session_id, error=str(exc))


async def _is_cancelled(session_id: str) -> bool:
    if not session_id:
        return False
    redis = get_redis()
    try:
        value = await redis.get(_cancel_key(session_id))
        return value is not None
    except Exception as exc:  # noqa: BLE001
        log.error("ai.stream.cancel_check_error", session_id=session_id, error=str(exc))
        return False


async def _clear_cancel(session_id: str) -> None:
    if not session_id:
        return
    redis = get_redis()
    try:
        await redis.delete(_cancel_key(session_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("ai.stream.cancel_clear_error", session_id=session_id, error=str(exc))


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION DU HANDLER
# ═══════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class StreamContext:
    """Paramètres contextuels d'un stream — passés au StreamHandler.

    `metrics` : si fourni, le StreamHandler l'enrichit au fil du stream au
    lieu de créer le sien. Permet au caller (router Chat) de lire à la fin
    le provider/model/usage/cost retenus et de finaliser le Message ORM en
    conséquence. Si None (par défaut), un StreamMetrics interne est créé
    puis émis normalement.
    """

    expert_id: str | None
    user_messages: list[ChatMessage]
    user_id: str
    trace_id: str
    session_id: str | None = None
    max_tokens: int | None = None
    metrics: StreamMetrics | None = None


# ═══════════════════════════════════════════════════════════════════
# HANDLER PRINCIPAL
# ═══════════════════════════════════════════════════════════════════


class StreamHandler:
    """Orchestre la résolution de la chaîne, l'appel provider, les
    fallbacks, le heartbeat et l'annulation. Produit un flux de chaînes
    SSE prêtes à être écrites dans une `StreamingResponse`.
    """

    def __init__(
        self,
        *,
        router: LlmRouter,
        retry_policy: RetryPolicy = DEFAULT_POLICY,
        breakers: CircuitBreakerRegistry | None = None,
    ) -> None:
        self._router = router
        self._retry_policy = retry_policy
        self._breakers = breakers or get_breaker_registry()

    async def stream(
        self,
        request: Request,
        ctx: StreamContext,
    ) -> AsyncIterator[str]:
        """Générateur SSE principal. À passer tel quel à `StreamingResponse`.

        Garanties :
        - Jamais lève : toutes les erreurs sont converties en événements SSE.
        - Émet toujours un `done` final (ou `error` + `done`).
        - Le heartbeat marche même pendant les retries.
        - L'annulation (disconnect client ou clé Redis) coupe net.
        """
        chain = self._router.build_chain(ctx.expert_id)
        started_at = time.monotonic()
        metrics = ctx.metrics or StreamMetrics(
            user_id=ctx.user_id,
            trace_id=ctx.trace_id,
            expert_id=ctx.expert_id,
            session_id=ctx.session_id,
        )

        log.info(
            "ai.stream.start",
            user_id=ctx.user_id,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            expert_id=ctx.expert_id,
            chain=[(l.provider.name, l.model) for l in chain],
        )

        if not chain:
            yield _sse(
                "error",
                {"code": "LLM_UNAVAILABLE", "message": "Aucun provider configuré."},
            )
            yield _sse("done", {"reason": "error"})
            metrics.finalize(outcome="failed", failure_code="LLM_UNAVAILABLE")
            metrics.emit()
            return

        cancel_scope = _CancelScope(request=request, session_id=ctx.session_id)
        cancel_scope.start()

        try:
            last_error: Exception | None = None
            for idx, link in enumerate(chain):
                if await cancel_scope.should_stop():
                    async for evt in self._emit_cancel(ctx):
                        yield evt
                    metrics.finalize(outcome="cancelled", failure_code="STREAM_CANCELLED")
                    metrics.emit()
                    return

                try:
                    async for evt in self._run_link(link, ctx, cancel_scope, idx, metrics):
                        yield evt
                    # Lien réussi → fin du stream, on sort de la boucle
                    yield _sse(
                        "done",
                        {"reason": "stop", "duration_ms": int((time.monotonic() - started_at) * 1000)},
                    )
                    metrics.finalize(outcome="success")
                    metrics.emit()
                    return
                except _ChainLinkFailed as exc:
                    last_error = exc.cause
                    log.warning(
                        "ai.stream.link_failed",
                        user_id=ctx.user_id,
                        trace_id=ctx.trace_id,
                        provider=link.provider.name,
                        model=link.model,
                        error_type=type(exc.cause).__name__,
                        fallback_remaining=len(chain) - idx - 1,
                    )
                    continue
                except _ChainCancelled:
                    async for evt in self._emit_cancel(ctx):
                        yield evt
                    metrics.finalize(outcome="cancelled", failure_code="STREAM_CANCELLED")
                    metrics.emit()
                    return

            # Chaîne épuisée sans succès
            log.error(
                "ai.stream.all_providers_failed",
                user_id=ctx.user_id,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                last_error=str(last_error) if last_error else None,
            )
            yield _sse(
                "error",
                {
                    "code": "LLM_UNAVAILABLE",
                    "message": (
                        "Le service IA est temporairement indisponible. "
                        "Réessaye dans quelques instants."
                    ),
                },
            )
            yield _sse(
                "done",
                {"reason": "error", "duration_ms": int((time.monotonic() - started_at) * 1000)},
            )
            metrics.finalize(outcome="failed", failure_code="LLM_UNAVAILABLE")
            metrics.emit()

        finally:
            await cancel_scope.stop()
            if ctx.session_id:
                await _clear_cancel(ctx.session_id)

    # ─── Exécution d'un lien de la chaîne ────────────────────────────

    async def _run_link(
        self,
        link: ChatResolution,
        ctx: StreamContext,
        cancel_scope: "_CancelScope",
        link_index: int,
        metrics: StreamMetrics,
    ) -> AsyncIterator[str]:
        provider = link.provider
        model = link.model
        config = link.config

        try:
            self._breakers.before_call(provider.name, model)
        except CircuitOpenError:
            raise _ChainLinkFailed(
                CircuitOpenError(
                    provider=provider.name, model=model, reopen_in_seconds=0.0
                )
            )

        metrics.bind_provider(provider.name, model, is_fallback=link_index > 0)

        # Ajouter disclaimer au premier chunk si applicable
        disclaimer_prefix = (config.disclaimer + "\n\n") if link_index == 0 and config.disclaimer else ""

        request = ChatCompletionRequest(
            messages=ctx.user_messages,
            system_prompt=config.system_prompt,
            model=model,
            temperature=config.temperature,
            max_tokens=ctx.max_tokens or config.max_tokens,
            user_id=ctx.user_id,
            trace_id=ctx.trace_id,
            expert_id=config.expert_id,
        )

        stream = stream_chat_with_retry(
            provider=provider,
            request=request,
            policy=self._retry_policy,
        )

        first_chunk = True
        last_keepalive = time.monotonic()

        try:
            async for chunk in _interleave_with_heartbeat(stream, cancel_scope):
                if chunk is _HEARTBEAT:
                    yield _keepalive_comment()
                    last_keepalive = time.monotonic()
                    continue
                if chunk is _CANCELLED:
                    raise _ChainCancelled()

                assert isinstance(chunk, ChatChunk)
                payload: dict = {}
                if first_chunk and disclaimer_prefix:
                    payload["delta"] = disclaimer_prefix + chunk.delta
                    first_chunk = False
                else:
                    payload["delta"] = chunk.delta
                    first_chunk = False

                if chunk.finish_reason is not None:
                    payload["finish_reason"] = chunk.finish_reason.value
                if chunk.usage is not None:
                    payload["usage"] = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }
                    metrics.usage = chunk.usage
                event_line = _sse("chunk", payload)
                metrics.record_chunk(size_bytes=len(event_line.encode("utf-8")))
                yield event_line

                # Force un keepalive si un chunk long a bouffé l'intervalle
                if time.monotonic() - last_keepalive >= HEARTBEAT_SECONDS:
                    yield _keepalive_comment()
                    last_keepalive = time.monotonic()

            self._breakers.record_success(provider.name, model)

        except asyncio.CancelledError:
            self._breakers.record_failure(
                provider.name,
                model,
                ProviderError("cancelled", provider=provider.name, model=model, retryable=True),
            )
            raise _ChainCancelled()
        except ProviderError as exc:
            self._breakers.record_failure(provider.name, model, exc)
            if exc.retryable:
                raise _ChainLinkFailed(exc)
            # Erreur non-retryable (auth, content_filter, invalid) → on coupe la chaîne net
            async for evt in self._emit_non_retryable(exc, provider.name, model):
                yield evt
            return

    async def _emit_non_retryable(
        self, exc: ProviderError, provider: str, model: str
    ) -> AsyncIterator[str]:
        code = "LLM_UNAVAILABLE"
        message = "Impossible de générer une réponse."
        exc_name = type(exc).__name__
        if exc_name == "ProviderContentFilteredError":
            code = "CONTENT_FILTERED"
            message = "Ta requête a été bloquée par le filtre de sécurité."
        elif exc_name == "ProviderAuthError":
            log.error(
                "ai.stream.auth_error",
                provider=provider,
                model=model,
                error=str(exc),
            )
            # Ne JAMAIS exposer "clé invalide" au client
        yield _sse("error", {"code": code, "message": message})

    async def _emit_cancel(self, ctx: StreamContext) -> AsyncIterator[str]:
        log.info(
            "ai.stream.cancelled",
            user_id=ctx.user_id,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
        yield _sse(
            "error",
            {"code": "STREAM_CANCELLED", "message": "Stream interrompu."},
        )
        yield _sse("done", {"reason": "cancelled"})


# ═══════════════════════════════════════════════════════════════════
# CANCEL SCOPE — watchdog Redis + disconnect client
# ═══════════════════════════════════════════════════════════════════


class _CancelScope:
    """Surveille en parallèle :
    - `request.is_disconnected()` via tâche async
    - clé Redis `chat:cancel:{session_id}` via polling
    Pose `cancelled=True` dès que l'une des deux remonte.
    """

    def __init__(self, *, request: Request, session_id: str | None) -> None:
        self._request = request
        self._session_id = session_id
        self._cancelled = False
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._watch_disconnect()))
        if self._session_id:
            self._tasks.append(asyncio.create_task(self._watch_redis()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    async def should_stop(self) -> bool:
        return self._cancelled

    async def _watch_disconnect(self) -> None:
        try:
            while not self._cancelled:
                if await self._request.is_disconnected():
                    self._cancelled = True
                    return
                await asyncio.sleep(DISCONNECT_CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def _watch_redis(self) -> None:
        try:
            while not self._cancelled:
                if await _is_cancelled(self._session_id or ""):
                    self._cancelled = True
                    return
                await asyncio.sleep(CANCEL_CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return


# ═══════════════════════════════════════════════════════════════════
# INTERLEAVE HEARTBEAT
# ═══════════════════════════════════════════════════════════════════


_HEARTBEAT = object()
_CANCELLED = object()


async def _interleave_with_heartbeat(
    source: AsyncIterator[ChatChunk],
    cancel_scope: _CancelScope,
) -> AsyncIterator[ChatChunk | object]:
    """Intercale des sentinelles HEARTBEAT dans le flux d'un générateur
    quand il silence pendant plus de `HEARTBEAT_SECONDS`. Remonte aussi
    une sentinelle CANCELLED si le cancel_scope passe à True.
    """
    iterator = source.__aiter__()
    while True:
        if await cancel_scope.should_stop():
            yield _CANCELLED
            return

        next_task = asyncio.create_task(_safe_anext(iterator))
        try:
            done, _ = await asyncio.wait(
                {next_task},
                timeout=HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                yield _HEARTBEAT
                continue

            result = next_task.result()
            if result is _StopSentinel:
                return
            if isinstance(result, BaseException):
                raise result
            yield result
        finally:
            if not next_task.done():
                next_task.cancel()
                try:
                    await next_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


_StopSentinel = object()


async def _safe_anext(iterator):
    """Récupère le prochain chunk ou retourne `_StopSentinel` en fin de
    stream. Les exceptions sont retournées comme valeurs pour que le
    `asyncio.wait` les livre proprement."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _StopSentinel
    except BaseException as exc:  # noqa: BLE001 — transit
        return exc


# ═══════════════════════════════════════════════════════════════════
# EXCEPTIONS INTERNES (flux de contrôle)
# ═══════════════════════════════════════════════════════════════════


class _ChainLinkFailed(Exception):
    """Un maillon de la chaîne a échoué sur une erreur retryable ou
    un circuit ouvert. Le caller doit essayer le suivant."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _ChainCancelled(Exception):
    """Le stream a été annulé par l'utilisateur (disconnect ou /chat/stop)."""
