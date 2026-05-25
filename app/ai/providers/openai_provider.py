"""
NEXYA Couche IA — Provider OpenAI (implémentation réelle).

Traduit les types neutres de `base.py` vers l'API OpenAI Chat Completions
en mode streaming (Server-Sent Events côté réseau, traduits par le SDK en
`AsyncStream[ChatCompletionChunk]`).

Modèles supportés :
- `gpt-4o-mini` (défaut, économique)
- `gpt-4o`, `gpt-4-turbo`
- `o1`, `o1-mini` (reasoning models — pas de temperature, pas de system)

Mapping d'erreurs (aligné sur `ProviderError`) :
- `AuthenticationError` (401) / `PermissionDeniedError` (403) → `ProviderAuthError`
- `RateLimitError` (429) → `ProviderRateLimitError` avec `retry-after`
- `BadRequestError` (400) content_filter → `ProviderContentFilteredError`
- `BadRequestError` (400) autre → `ProviderInvalidRequestError`
- `APIConnectionError`, `InternalServerError`, `APITimeoutError`, 5xx →
  `ProviderUnavailableError` (retryable)
- `asyncio.CancelledError` : toujours propagé.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings

from .base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatProvider,
    ChatUsage,
    FinishReason,
    ProviderAuthError,
    ProviderCapability,
    ProviderContentFilteredError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    ToolCallDelta,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


log = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════
# CLIENT SINGLETON — construit paresseusement
# ═══════════════════════════════════════════════════════════════════


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        from openai import AsyncOpenAI  # import local — dépendance lourde

        if not settings.openai_api_key:
            # Ne devrait jamais arriver : la factory instancie un Mock si
            # la clé est vide. Garde-fou défensif.
            raise ProviderAuthError(
                "OPENAI_API_KEY est vide — configure la clé avant d'instancier le provider.",
                provider="openai",
            )
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=60.0,
            max_retries=0,  # on gère les retries dans app/ai/retry.py
        )
        log.info("ai.provider.openai.client_initialized")
    return _client


def _reset_client_for_tests() -> None:
    """Tests only — force la re-création du client (après monkeypatch settings)."""
    global _client
    _client = None


# ═══════════════════════════════════════════════════════════════════
# MAPPING DES ERREURS SDK OpenAI → ProviderError
# ═══════════════════════════════════════════════════════════════════


def _map_sdk_exception(exc: Exception, *, model: str) -> ProviderError:
    """Traduit une exception du SDK openai en `ProviderError` typée.

    Se base sur la hiérarchie `openai.APIError` — import local pour ne pas
    faire dépendre ce module du SDK au chargement (utile si les tests
    monkeypatchent le client).
    """
    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
    )

    message = str(exc) or exc.__class__.__name__

    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return ProviderAuthError(message, provider="openai", model=model)

    if isinstance(exc, RateLimitError):
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None:
            raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (ValueError, TypeError):
                    retry_after = None
        return ProviderRateLimitError(
            message, provider="openai", model=model, retry_after_seconds=retry_after
        )

    if isinstance(exc, BadRequestError):
        lower = message.lower()
        if "content_filter" in lower or "safety" in lower or "policy" in lower:
            return ProviderContentFilteredError(message, provider="openai", model=model)
        return ProviderInvalidRequestError(message, provider="openai", model=model)

    if isinstance(exc, NotFoundError):
        return ProviderInvalidRequestError(message, provider="openai", model=model)

    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return ProviderUnavailableError(message, provider="openai", model=model)

    status_code = getattr(exc, "status_code", None)
    return ProviderUnavailableError(
        message, provider="openai", model=model, status_code=status_code
    )


# ═══════════════════════════════════════════════════════════════════
# OpenAIChatProvider — streaming chat
# ═══════════════════════════════════════════════════════════════════


_REASONING_MODELS = frozenset({"o1", "o1-mini"})


class OpenAIChatProvider(ChatProvider):
    """Adaptateur OpenAI pour Chat Completions en streaming."""

    name = "openai"
    default_model = "gpt-4o-mini"
    supported_models = frozenset(
        {
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "o1",
            "o1-mini",
        }
    )
    capabilities = frozenset(
        {
            ProviderCapability.TEXT_CHAT,
            ProviderCapability.VISION,
            ProviderCapability.FUNCTION_CALLING,
            ProviderCapability.JSON_MODE,
        }
    )
    max_context_tokens = 128_000

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncIterator[ChatChunk]:
        model = request.model or self.default_model
        if not self.supports_model(model):
            raise ProviderInvalidRequestError(
                f"Modèle '{model}' non supporté par le provider OpenAI.",
                provider=self.name,
                model=model,
            )

        messages = _build_openai_messages(request, model=model)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Les reasoning models (o1) rejettent `temperature` et `system`.
        if model not in _REASONING_MODELS:
            kwargs["temperature"] = request.temperature
            if request.stop_sequences:
                kwargs["stop"] = list(request.stop_sequences)
        if request.max_tokens is not None:
            # o1 utilise `max_completion_tokens` ; gpt-4o utilise `max_tokens`.
            if model in _REASONING_MODELS:
                kwargs["max_completion_tokens"] = request.max_tokens
            else:
                kwargs["max_tokens"] = request.max_tokens
        if request.user_id:
            kwargs["user"] = request.user_id

        # F2.5 — function calling. Le format `request.tools` est déjà au
        # format OpenAI natif (`{"type": "function", "function": {...}}`),
        # on le passe tel quel. `tool_choice="auto"` laisse le LLM décider
        # si un tool est pertinent ou non — alternative `"required"` force
        # un appel, `"none"` désactive (équivalent à ne pas envoyer `tools`).
        if request.tools:
            kwargs["tools"] = list(request.tools)
            # [planner-from-chat LOT 5] — "required" force un appel de tool
            # quand l'intent classifier a détecté une planification claire
            # (round 0, `request.extra["force_tool_call"]`). Sinon "auto" :
            # le LLM décide.
            kwargs["tool_choice"] = "required" if request.extra.get("force_tool_call") else "auto"

        client = _get_client()

        try:
            stream = await client.chat.completions.create(**kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        last_usage: ChatUsage | None = None
        last_finish: FinishReason | None = None
        produced_any = False

        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    last_usage = _extract_openai_usage(usage)

                choices = getattr(chunk, "choices", None) or []
                if choices:
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if text:
                        produced_any = True
                        yield ChatChunk(delta=text)

                    # F2.5 — tool_calls streamés en deltas indexés.
                    # Format OpenAI : delta.tool_calls = [{index, id?,
                    # function: {name?, arguments}}]. `id` et `name` ne sont
                    # présents qu'au premier delta de chaque tool ; les
                    # `arguments` arrivent fragmentés sur plusieurs deltas.
                    # Un appel parallèle (parallel_tool_calls) produit
                    # plusieurs index distincts dans le même chunk.
                    raw_tool_calls = (
                        getattr(delta, "tool_calls", None) if delta is not None else None
                    )
                    if raw_tool_calls:
                        for tc in raw_tool_calls:
                            tc_index = getattr(tc, "index", 0) or 0
                            tc_id = getattr(tc, "id", None) or None
                            fn = getattr(tc, "function", None)
                            tc_name = getattr(fn, "name", None) if fn is not None else None
                            tc_args = getattr(fn, "arguments", None) if fn is not None else None
                            yield ChatChunk(
                                tool_call=ToolCallDelta(
                                    id=tc_id,
                                    name=tc_name,
                                    arguments_json_partial=tc_args or "",
                                    index=int(tc_index),
                                )
                            )

                    raw_finish = getattr(choice, "finish_reason", None)
                    if raw_finish is not None:
                        last_finish = _map_openai_finish_reason(raw_finish)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        if last_finish is None:
            last_finish = FinishReason.STOP if produced_any else FinishReason.ERROR
        yield ChatChunk(delta="", finish_reason=last_finish, usage=last_usage)

    async def health_check(self) -> bool:
        try:
            client = _get_client()
            await client.models.list()
            return True
        except Exception:  # noqa: BLE001
            return False


# ═══════════════════════════════════════════════════════════════════
# HELPERS DE TRADUCTION
# ═══════════════════════════════════════════════════════════════════


def _build_openai_messages(request: ChatCompletionRequest, *, model: str) -> list[dict[str, Any]]:
    """Convertit `ChatCompletionRequest` vers la liste OpenAI.

    - Les `system_prompt` et messages `role="system"` sont envoyés en
      premier comme message `system` — sauf pour les reasoning models (o1)
      qui rejettent le rôle `system` (OpenAI recommande de les fusionner
      dans le premier message `user`).
    """
    is_reasoning = model in _REASONING_MODELS
    system_parts: list[str] = []
    if request.system_prompt:
        system_parts.append(request.system_prompt.strip())

    openai_messages: list[dict[str, Any]] = []
    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content.strip())
            continue
        openai_messages.append({"role": msg.role, "content": msg.content})

    if not is_reasoning and system_parts:
        openai_messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
    elif is_reasoning and system_parts and openai_messages:
        # Pour o1 : préfixer le premier message user avec les instructions system.
        first_user_idx = next(
            (i for i, m in enumerate(openai_messages) if m["role"] == "user"), None
        )
        if first_user_idx is not None:
            prefix = "\n\n".join(system_parts)
            openai_messages[first_user_idx]["content"] = (
                f"{prefix}\n\n{openai_messages[first_user_idx]['content']}"
            )

    return openai_messages


def _extract_openai_usage(usage_obj: Any) -> ChatUsage | None:
    try:
        prompt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        total = int(getattr(usage_obj, "total_tokens", 0) or (prompt + completion))
        if prompt == 0 and completion == 0:
            return None
        return ChatUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )
    except Exception:  # noqa: BLE001
        return None


def _map_openai_finish_reason(raw: Any) -> FinishReason:
    name = str(raw).lower()
    if name == "stop":
        return FinishReason.STOP
    if name == "length":
        return FinishReason.LENGTH
    if name in ("content_filter", "content-filter"):
        return FinishReason.CONTENT_FILTER
    # F2.5 — function calling : `tool_calls` (et legacy `function_call`)
    # signale au caller que le modèle veut appeler un ou plusieurs tools.
    # L'orchestrateur `run_with_tool_rounds` détecte ce finish_reason pour
    # exécuter les handlers via le ToolRegistry et ré-injecter les résultats.
    if name in ("tool_calls", "function_call"):
        return FinishReason.TOOL_CALLS
    return FinishReason.STOP
