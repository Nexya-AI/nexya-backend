"""
NEXYA Couche IA — Provider Qwen (Alibaba DashScope, implémentation réelle).

Qwen 2.5 est notre candidat pour les langues africaines (meilleur que Gemma
sur les benchmarks 2026). DashScope International expose un endpoint
**compatible OpenAI** : on réutilise le SDK `openai` avec un `base_url`
override plutôt que de gérer un client HTTP maison.

Endpoint : `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
(région US, disponible hors Chine continentale).

Modèles supportés (instruct, fine-tune NEXYA prévu en bloc H) :
- `qwen2.5-72b-instruct` (défaut, top qualité)
- `qwen2.5-32b-instruct`, `qwen2.5-14b-instruct`, `qwen2.5-7b-instruct`
- `qwen-max` (service premium propriétaire)

Mapping d'erreurs : identique à OpenAI (même SDK, mêmes classes d'erreur).
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


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        from openai import AsyncOpenAI  # import local

        if not settings.qwen_api_key:
            raise ProviderAuthError(
                "QWEN_API_KEY est vide — configure la clé DashScope avant d'instancier le provider.",
                provider="qwen",
            )
        _client = AsyncOpenAI(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            timeout=60.0,
            max_retries=0,
        )
        log.info("ai.provider.qwen.client_initialized", base_url=settings.qwen_base_url)
    return _client


def _reset_client_for_tests() -> None:
    global _client
    _client = None


def _map_sdk_exception(exc: Exception, *, model: str) -> ProviderError:
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
    lower = message.lower()

    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return ProviderAuthError(message, provider="qwen", model=model)

    if isinstance(exc, RateLimitError):
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None and hasattr(response, "headers"):
            raw = response.headers.get("retry-after")
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (ValueError, TypeError):
                    retry_after = None
        return ProviderRateLimitError(
            message, provider="qwen", model=model, retry_after_seconds=retry_after
        )

    if isinstance(exc, BadRequestError):
        if "content_filter" in lower or "safety" in lower or "blocked" in lower:
            return ProviderContentFilteredError(message, provider="qwen", model=model)
        return ProviderInvalidRequestError(message, provider="qwen", model=model)

    if isinstance(exc, NotFoundError):
        return ProviderInvalidRequestError(message, provider="qwen", model=model)

    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return ProviderUnavailableError(message, provider="qwen", model=model)

    status_code = getattr(exc, "status_code", None)
    return ProviderUnavailableError(message, provider="qwen", model=model, status_code=status_code)


class QwenChatProvider(ChatProvider):
    """Adaptateur Qwen via endpoint DashScope compatible OpenAI."""

    name = "qwen"
    default_model = "qwen2.5-72b-instruct"
    supported_models = frozenset(
        {
            "qwen2.5-72b-instruct",
            "qwen2.5-32b-instruct",
            "qwen2.5-14b-instruct",
            "qwen2.5-7b-instruct",
            "qwen-max",
        }
    )
    capabilities = frozenset(
        {
            ProviderCapability.TEXT_CHAT,
            ProviderCapability.FUNCTION_CALLING,
            ProviderCapability.JSON_MODE,
        }
    )
    max_context_tokens = 128_000

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncIterator[ChatChunk]:
        model = request.model or self.default_model
        if not self.supports_model(model):
            raise ProviderInvalidRequestError(
                f"Modèle '{model}' non supporté par le provider Qwen.",
                provider=self.name,
                model=model,
            )

        messages = _build_qwen_messages(request)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.stop_sequences:
            kwargs["stop"] = list(request.stop_sequences)

        # F2.5 — function calling. Qwen via DashScope expose le même
        # contrat que OpenAI (compat SDK), on passe `tools` + `tool_choice`
        # tel quel. Si DashScope rejette le format, on remontera via
        # `_map_sdk_exception` en `ProviderInvalidRequestError`.
        if request.tools:
            kwargs["tools"] = list(request.tools)
            kwargs["tool_choice"] = "auto"

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
                    last_usage = _extract_qwen_usage(usage)

                choices = getattr(chunk, "choices", None) or []
                if choices:
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if text:
                        produced_any = True
                        yield ChatChunk(delta=text)

                    # F2.5 — tool_calls streamés (format OpenAI-compat).
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
                        last_finish = _map_qwen_finish_reason(raw_finish)
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
# HELPERS
# ═══════════════════════════════════════════════════════════════════


def _build_qwen_messages(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    """Qwen utilise le format OpenAI — avec system en premier message."""
    system_parts: list[str] = []
    if request.system_prompt:
        system_parts.append(request.system_prompt.strip())

    qwen_messages: list[dict[str, Any]] = []
    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content.strip())
            continue
        qwen_messages.append({"role": msg.role, "content": msg.content})

    if system_parts:
        qwen_messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    return qwen_messages


def _extract_qwen_usage(usage_obj: Any) -> ChatUsage | None:
    try:
        prompt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        total = int(getattr(usage_obj, "total_tokens", 0) or (prompt + completion))
        if prompt == 0 and completion == 0:
            return None
        return ChatUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
    except Exception:  # noqa: BLE001
        return None


def _map_qwen_finish_reason(raw: Any) -> FinishReason:
    name = str(raw).lower()
    if name == "stop":
        return FinishReason.STOP
    if name == "length":
        return FinishReason.LENGTH
    if name in ("content_filter", "content-filter"):
        return FinishReason.CONTENT_FILTER
    if name in ("tool_calls", "function_call"):
        return FinishReason.TOOL_CALLS
    return FinishReason.STOP
