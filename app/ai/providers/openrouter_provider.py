"""
NEXYA Couche IA — Provider OpenRouter (agrégateur multi-modèles, implémentation réelle).

OpenRouter est un **agrégateur** qui expose sous une seule API OpenAI-compatible
des dizaines de modèles propriétaires et open-source (Anthropic, Meta, Mistral,
DeepSeek, Qwen, Cohere, Google, etc.) — avec routage automatique, paiement
unique, et bascule entre providers si l'un est down.

Pourquoi c'est utile pour NEXYA :
- **Second fallback généraliste**. Dès qu'OpenAI/Anthropic/Qwen sont tous KO
  (rare mais possible — blackout AWS, Azure, GCP), OpenRouter sert de roue
  de secours sur `general`, `productivity`, `sciences`. On évite un
  `LLM_UNAVAILABLE` sur le mode principal utilisé par 70 % des users.
- **Accès expérimental à des modèles qu'on ne veut pas intégrer directement**.
  Fine-tunes communautaires, modèles open-source exotiques — utiles pour A/B
  tests sans ouvrir un nouveau compte chez chaque fournisseur.
- **Jamais sur safety-critical**. On NE met PAS OpenRouter en fallback sur
  `medicine` / `legal` — l'agrégateur peut router vers un modèle communautaire
  dont on n'a pas vérifié l'alignement. Les domaines sensibles restent sur
  Gemini (primaire) et OpenAI/Anthropic (fallbacks directs connus).

Endpoint : `https://openrouter.ai/api/v1` (compatible OpenAI, en-têtes
optionnels `HTTP-Referer` + `X-Title` pour identifier NEXYA dans les
dashboards OpenRouter).

Modèles curés pour NEXYA (liste volontairement courte — on évite le choix
paralysant) :
- `anthropic/claude-3.5-sonnet` (défaut — qualité/coût, généraliste)
- `meta-llama/llama-3.1-70b-instruct` (alternative open-source solide)
- `mistralai/mistral-large` (européen, bon en FR)
- `deepseek/deepseek-chat` (rapport qualité/prix imbattable)
- `qwen/qwen-2.5-72b-instruct` (alternative à notre DashScope)

Mapping d'erreurs : identique à OpenAI (même SDK, mêmes classes d'erreurs).
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
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI


log = structlog.get_logger()


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        from openai import AsyncOpenAI  # import local

        if not settings.openrouter_api_key:
            raise ProviderAuthError(
                "OPENROUTER_API_KEY est vide — configure la clé OpenRouter avant "
                "d'instancier le provider.",
                provider="openrouter",
            )

        # En-têtes optionnels OpenRouter : permettent à l'équipe NEXYA
        # d'identifier l'appli dans son dashboard. Non bloquants — si les
        # settings `openrouter_referer` / `openrouter_app_title` sont vides,
        # on n'envoie rien et OpenRouter accepte quand même.
        default_headers: dict[str, str] = {}
        if settings.openrouter_referer:
            default_headers["HTTP-Referer"] = settings.openrouter_referer
        if settings.openrouter_app_title:
            default_headers["X-Title"] = settings.openrouter_app_title

        _client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            timeout=60.0,
            max_retries=0,
            default_headers=default_headers or None,
        )
        log.info(
            "ai.provider.openrouter.client_initialized",
            base_url=settings.openrouter_base_url,
            has_referer=bool(settings.openrouter_referer),
            has_title=bool(settings.openrouter_app_title),
        )
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
        return ProviderAuthError(message, provider="openrouter", model=model)

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
            message,
            provider="openrouter",
            model=model,
            retry_after_seconds=retry_after,
        )

    if isinstance(exc, BadRequestError):
        if (
            "content_filter" in lower
            or "safety" in lower
            or "blocked" in lower
            or "moderation" in lower
        ):
            return ProviderContentFilteredError(message, provider="openrouter", model=model)
        return ProviderInvalidRequestError(message, provider="openrouter", model=model)

    if isinstance(exc, NotFoundError):
        return ProviderInvalidRequestError(message, provider="openrouter", model=model)

    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return ProviderUnavailableError(message, provider="openrouter", model=model)

    status_code = getattr(exc, "status_code", None)
    return ProviderUnavailableError(
        message, provider="openrouter", model=model, status_code=status_code
    )


class OpenRouterChatProvider(ChatProvider):
    """Adaptateur OpenRouter via endpoint compatible OpenAI."""

    name = "openrouter"
    default_model = "anthropic/claude-3.5-sonnet"
    supported_models = frozenset(
        {
            "anthropic/claude-3.5-sonnet",
            "meta-llama/llama-3.1-70b-instruct",
            "mistralai/mistral-large",
            "deepseek/deepseek-chat",
            "qwen/qwen-2.5-72b-instruct",
        }
    )
    capabilities = frozenset(
        {
            ProviderCapability.TEXT_CHAT,
            ProviderCapability.JSON_MODE,
        }
    )
    # OpenRouter expose des modèles avec des fenêtres variables (128k→200k
    # selon le modèle sous-jacent). On aligne sur le plus commun (128k) —
    # le token_estimator fait le bon cap applicatif en amont.
    max_context_tokens = 128_000

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncIterator[ChatChunk]:
        model = request.model or self.default_model
        if not self.supports_model(model):
            raise ProviderInvalidRequestError(
                f"Modèle '{model}' non supporté par le provider OpenRouter.",
                provider=self.name,
                model=model,
            )

        messages = _build_openrouter_messages(request)
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
                    last_usage = _extract_openrouter_usage(usage)

                choices = getattr(chunk, "choices", None) or []
                if choices:
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if text:
                        produced_any = True
                        yield ChatChunk(delta=text)
                    raw_finish = getattr(choice, "finish_reason", None)
                    if raw_finish is not None:
                        last_finish = _map_openrouter_finish_reason(raw_finish)
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


def _build_openrouter_messages(
    request: ChatCompletionRequest,
) -> list[dict[str, Any]]:
    """OpenRouter accepte le format OpenAI — avec system en premier message."""
    system_parts: list[str] = []
    if request.system_prompt:
        system_parts.append(request.system_prompt.strip())

    messages: list[dict[str, Any]] = []
    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content.strip())
            continue
        messages.append({"role": msg.role, "content": msg.content})

    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    return messages


def _extract_openrouter_usage(usage_obj: Any) -> ChatUsage | None:
    try:
        prompt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        total = int(getattr(usage_obj, "total_tokens", 0) or (prompt + completion))
        if prompt == 0 and completion == 0:
            return None
        return ChatUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)
    except Exception:  # noqa: BLE001
        return None


def _map_openrouter_finish_reason(raw: Any) -> FinishReason:
    name = str(raw).lower()
    if name == "stop":
        return FinishReason.STOP
    if name == "length":
        return FinishReason.LENGTH
    if name in ("content_filter", "content-filter"):
        return FinishReason.CONTENT_FILTER
    return FinishReason.STOP
