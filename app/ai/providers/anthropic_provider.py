"""
NEXYA Couche IA — Provider Anthropic (Claude, implémentation réelle).

Adaptateur vers `anthropic.AsyncAnthropic` en mode streaming via le
context manager `client.messages.stream(...)`.

Spécificités Claude :
- Le `system_prompt` est un **paramètre séparé**, jamais un message avec
  `role="system"` (l'API rejette ce rôle dans le tableau `messages`).
- L'API requiert un `max_tokens` non-nul — on impose un plafond par défaut
  raisonnable (4096) si le caller n'en donne pas.
- Les `stop_reason` possibles : "end_turn", "max_tokens", "stop_sequence",
  "tool_use". Mapping → `FinishReason` neutre.

Modèles supportés :
- `claude-sonnet-4-6` (défaut, équilibre coût/qualité)
- `claude-opus-4-6` (top raisonnement)
- `claude-haiku-4-5` (rapide, économique)

Mapping d'erreurs (aligné sur `ProviderError`) :
- `AuthenticationError` (401) / `PermissionDeniedError` (403) → `ProviderAuthError`
- `RateLimitError` (429) → `ProviderRateLimitError` avec `retry-after`
- `BadRequestError` (400) + `invalid_request_error` content → `ProviderInvalidRequestError`
- `BadRequestError` (400) + safety/policy → `ProviderContentFilteredError`
- `APIConnectionError`, `APITimeoutError`, `InternalServerError`, 5xx →
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
    from anthropic import AsyncAnthropic


log = structlog.get_logger()


_DEFAULT_MAX_TOKENS = 4096


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic  # import local

        if not settings.anthropic_api_key:
            raise ProviderAuthError(
                "ANTHROPIC_API_KEY est vide — configure la clé avant d'instancier le provider.",
                provider="anthropic",
            )
        _client = AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=60.0,
            max_retries=0,
        )
        log.info("ai.provider.anthropic.client_initialized")
    return _client


def _reset_client_for_tests() -> None:
    global _client
    _client = None


def _map_sdk_exception(exc: Exception, *, model: str) -> ProviderError:
    from anthropic import (
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
        return ProviderAuthError(message, provider="anthropic", model=model)

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
            message, provider="anthropic", model=model, retry_after_seconds=retry_after
        )

    if isinstance(exc, BadRequestError):
        if "safety" in lower or "policy" in lower or "blocked" in lower:
            return ProviderContentFilteredError(message, provider="anthropic", model=model)
        return ProviderInvalidRequestError(message, provider="anthropic", model=model)

    if isinstance(exc, NotFoundError):
        return ProviderInvalidRequestError(message, provider="anthropic", model=model)

    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError)):
        return ProviderUnavailableError(message, provider="anthropic", model=model)

    status_code = getattr(exc, "status_code", None)
    return ProviderUnavailableError(
        message, provider="anthropic", model=model, status_code=status_code
    )


class AnthropicChatProvider(ChatProvider):
    """Adaptateur Claude via `anthropic.AsyncAnthropic`."""

    name = "anthropic"
    default_model = "claude-sonnet-4-6"
    supported_models = frozenset(
        {
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
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
    max_context_tokens = 200_000

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncIterator[ChatChunk]:
        model = request.model or self.default_model
        if not self.supports_model(model):
            raise ProviderInvalidRequestError(
                f"Modèle '{model}' non supporté par le provider Anthropic.",
                provider=self.name,
                model=model,
            )

        system_prompt, messages = _build_claude_messages(request)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
            "temperature": request.temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if request.stop_sequences:
            kwargs["stop_sequences"] = list(request.stop_sequences)

        # F2.5 — function calling. Format spécifique Anthropic :
        # `tools=[{name, description, input_schema}]` (pas de wrapper
        # `{type: "function", function: {...}}` comme OpenAI ; champ
        # `input_schema` au lieu de `parameters`). Le helper réécrit le
        # format OpenAI natif vers le format Anthropic en mémoire (sans
        # muter `request.tools`).
        if request.tools:
            anthropic_tools = _to_anthropic_tools(request.tools)
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools
                # [planner-from-chat LOT 5] — {"type": "any"} force un appel
                # de tool quand l'intent classifier a détecté une
                # planification claire (round 0,
                # `request.extra["force_tool_call"]`) ; sinon {"type": "auto"}.
                kwargs["tool_choice"] = (
                    {"type": "any"}
                    if request.extra.get("force_tool_call")
                    else {"type": "auto"}
                )

        client = _get_client()

        last_usage: ChatUsage | None = None
        last_finish: FinishReason | None = None
        produced_any = False

        # F2.5 — accumulateur tool_use par index. Anthropic streame
        # `content_block_start` (avec id+name au début) puis plusieurs
        # `content_block_delta` de type `input_json_delta` (fragments
        # `partial_json` de la chaîne JSON des arguments).
        tool_use_blocks: dict[int, dict[str, str | None]] = {}

        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", None)

                    if event_type == "content_block_start":
                        # Détecte un nouveau bloc tool_use et émet un
                        # premier ChatChunk avec id + name.
                        block = getattr(event, "content_block", None)
                        block_type = getattr(block, "type", None) if block is not None else None
                        if block_type == "tool_use":
                            block_index = int(getattr(event, "index", 0) or 0)
                            tool_id = getattr(block, "id", None)
                            tool_name = getattr(block, "name", None)
                            tool_use_blocks[block_index] = {
                                "id": tool_id,
                                "name": tool_name,
                            }
                            yield ChatChunk(
                                tool_call=ToolCallDelta(
                                    id=tool_id,
                                    name=tool_name,
                                    arguments_json_partial="",
                                    index=block_index,
                                )
                            )

                    elif event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", None) if delta is not None else None

                        if delta_type == "input_json_delta":
                            # Fragment JSON des arguments d'un tool_use.
                            block_index = int(getattr(event, "index", 0) or 0)
                            partial = getattr(delta, "partial_json", "") or ""
                            block_meta = tool_use_blocks.get(block_index, {})
                            yield ChatChunk(
                                tool_call=ToolCallDelta(
                                    id=None,  # déjà émis au content_block_start
                                    name=None,
                                    arguments_json_partial=partial,
                                    index=block_index,
                                )
                            )
                            # silence unused pour clarifier le tracking
                            _ = block_meta
                            continue

                        text = getattr(delta, "text", None) if delta is not None else None
                        if text:
                            produced_any = True
                            yield ChatChunk(delta=text)

                    elif event_type == "message_delta":
                        # Porte `stop_reason` final et une partie de l'usage
                        delta = getattr(event, "delta", None)
                        stop_reason = (
                            getattr(delta, "stop_reason", None) if delta is not None else None
                        )
                        if stop_reason is not None:
                            last_finish = _map_claude_stop_reason(stop_reason)
                        usage = getattr(event, "usage", None)
                        if usage is not None:
                            last_usage = _merge_claude_usage(last_usage, usage)

                    elif event_type == "message_stop":
                        # Message final — usage complète disponible via `stream.get_final_message()`
                        try:
                            final_message = await stream.get_final_message()
                        except Exception:  # noqa: BLE001
                            final_message = None
                        if final_message is not None:
                            final_usage = getattr(final_message, "usage", None)
                            if final_usage is not None:
                                last_usage = _extract_claude_usage(final_usage)
                            final_stop = getattr(final_message, "stop_reason", None)
                            if final_stop is not None and last_finish is None:
                                last_finish = _map_claude_stop_reason(final_stop)
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
            # Anthropic n'expose pas de `/models`. On fait un appel trivial
            # avec max_tokens=1 sur le modèle le plus cheap.
            await client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:  # noqa: BLE001
            return False


# ═══════════════════════════════════════════════════════════════════
# HELPERS DE TRADUCTION
# ═══════════════════════════════════════════════════════════════════


def _build_claude_messages(
    request: ChatCompletionRequest,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Retourne `(system_prompt, messages)` au format Anthropic.

    - Les `role="system"` inline sont fusionnés dans `system_prompt`.
    - Claude exige au moins un message user. Si la liste est vide, on
      laisse le caller gérer (ProviderInvalidRequestError remonté par
      l'API Anthropic).
    - Les rôles ne peuvent pas alterner librement : Claude attend user/
      assistant/user/... mais le SDK tolère les séquences user/user (il
      les fusionne). On envoie tel quel.
    """
    system_parts: list[str] = []
    if request.system_prompt:
        system_parts.append(request.system_prompt.strip())

    messages: list[dict[str, Any]] = []
    for msg in request.messages:
        if msg.role == "system":
            system_parts.append(msg.content.strip())
            continue
        messages.append({"role": msg.role, "content": msg.content})

    system_prompt = "\n\n".join(p for p in system_parts if p) or None
    return system_prompt, messages


def _extract_claude_usage(usage_obj: Any) -> ChatUsage | None:
    try:
        prompt = int(getattr(usage_obj, "input_tokens", 0) or 0)
        completion = int(getattr(usage_obj, "output_tokens", 0) or 0)
        if prompt == 0 and completion == 0:
            return None
        return ChatUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )
    except Exception:  # noqa: BLE001
        return None


def _merge_claude_usage(previous: ChatUsage | None, new_obj: Any) -> ChatUsage | None:
    """Anthropic envoie l'usage en plusieurs morceaux : `message_start`
    porte `input_tokens`, `message_delta` porte les `output_tokens` au
    fur et à mesure. On accumule en gardant le max (les deltas sont
    cumulatifs côté SDK)."""
    new_usage = _extract_claude_usage(new_obj)
    if new_usage is None:
        return previous
    if previous is None:
        return new_usage
    return ChatUsage(
        prompt_tokens=max(previous.prompt_tokens, new_usage.prompt_tokens),
        completion_tokens=max(previous.completion_tokens, new_usage.completion_tokens),
        total_tokens=max(previous.total_tokens, new_usage.total_tokens),
    )


def _map_claude_stop_reason(raw: Any) -> FinishReason:
    name = str(raw).lower()
    if name == "end_turn":
        return FinishReason.STOP
    if name == "max_tokens":
        return FinishReason.LENGTH
    if name == "stop_sequence":
        return FinishReason.STOP
    # F2.5 — Claude veut appeler un ou plusieurs tools.
    if name == "tool_use":
        return FinishReason.TOOL_CALLS
    return FinishReason.ERROR


# ═══════════════════════════════════════════════════════════════════
# F2.5 — Helper : OpenAI tools format → Anthropic tools format
# ═══════════════════════════════════════════════════════════════════


def _to_anthropic_tools(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convertit le format OpenAI natif vers le format Anthropic.

    Entrée :
        [{"type": "function",
          "function": {"name": ..., "description": ..., "parameters": {...}}}]

    Sortie :
        [{"name": ..., "description": ..., "input_schema": {...}}]

    Tolérant : un dict d'entrée mal formé (`function` absent, `name` vide…)
    est silencieusement ignoré plutôt que de crasher l'appel — un Tool
    invalide ne doit pas empêcher les autres de fonctionner.
    """
    out: list[dict[str, Any]] = []
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") if raw.get("type") == "function" else raw
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        anthropic_tool: dict[str, Any] = {"name": name}
        if "description" in fn:
            anthropic_tool["description"] = fn["description"]
        # `parameters` (OpenAI) → `input_schema` (Anthropic). Anthropic
        # exige toujours un schéma, même s'il est vide ; on défalque sur
        # `{"type": "object", "properties": {}}` pour rester compatible.
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        anthropic_tool["input_schema"] = schema
        out.append(anthropic_tool)
    return out
