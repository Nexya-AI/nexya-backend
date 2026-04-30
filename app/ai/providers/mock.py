"""
NEXYA Couche IA — MockChatProvider.

Fallback utilisé quand un provider réel n'a pas de clé API configurée (dev,
tests, environnement de démo). Produit un stream texte prédictible avec des
tokens découpés, un `FinishReason.STOP`, et un `ChatUsage` cohérent pour que
toute la chaîne aval (observabilité, cost tracker, StreamHandler) puisse
s'exercer sans appeler d'IA réelle.

Deux usages :
1. **Factory** : `build_default_router()` instancie un `MockChatProvider`
   portant le `name` / `default_model` d'un provider réel (ex: "openai")
   si la clé correspondante est vide. Le router et les chaînes de fallback
   continuent de fonctionner comme si le provider était branché.
2. **Tests** : on peut passer `force_fail=ProviderRateLimitError(...)` pour
   simuler une erreur, ou `scripted_chunks=[...]` pour contrôler le flux.

Design choices :
- `min_chunk_delay_seconds` permet d'imiter un vrai stream asynchrone dans
  les tests d'intégration du `StreamHandler` (annulation, heartbeat).
- `name` et `default_model` sont injectables : un seul fichier sert pour
  openai/anthropic/qwen/gemini/n'importe quel alias à venir.
- `asyncio.CancelledError` propagé sans conversion (contrat ChatProvider).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

import structlog

from .base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatProvider,
    ChatUsage,
    FinishReason,
    ProviderCapability,
    ProviderError,
    ProviderInvalidRequestError,
    ToolCallDelta,
)

log = structlog.get_logger()


_DEFAULT_REPLY = (
    "Bonjour. Ce message provient du MockChatProvider NEXYA : le provider "
    "réel n'a pas encore de clé API configurée, mais la chaîne de streaming "
    "fonctionne de bout en bout."
)


def _default_chunks(reply: str) -> list[str]:
    """Découpe un texte en morceaux de ~20 caractères pour simuler du streaming."""
    if not reply:
        return [""]
    size = 20
    return [reply[i : i + size] for i in range(0, len(reply), size)]


class MockChatProvider(ChatProvider):
    """Provider factice — conforme au contrat `ChatProvider`.

    Paramètres du constructeur :
    - `name` / `default_model` / `supported_models` / `max_context_tokens` :
      surchargent les class-vars pour permettre d'usurper l'identité d'un
      provider réel (ex: `name="openai"`).
    - `scripted_chunks` : si fourni, joue cette liste au lieu du texte par défaut.
    - `min_chunk_delay_seconds` : pause entre chunks (défaut 0 — instantané).
    - `force_fail` : si fourni, lève cette exception avant de yield (simule panne).
    - `echo_user_message` : si True, préfixe la réponse du dernier message user
      (utile en dev pour voir que la chaîne fonctionne).
    """

    name: str = "mock"
    default_model: str = "mock-default"
    supported_models: frozenset[str] = frozenset({"mock-default"})
    capabilities: frozenset[ProviderCapability] = frozenset({ProviderCapability.TEXT_CHAT})
    max_context_tokens: int = 100_000

    def __init__(
        self,
        *,
        name: str | None = None,
        default_model: str | None = None,
        supported_models: Sequence[str] | None = None,
        max_context_tokens: int | None = None,
        scripted_chunks: Sequence[str] | None = None,
        min_chunk_delay_seconds: float = 0.0,
        force_fail: ProviderError | None = None,
        echo_user_message: bool = False,
        scripted_tool_call: dict | None = None,
    ) -> None:
        if name is not None:
            self.name = name
        if default_model is not None:
            self.default_model = default_model
        if supported_models is not None:
            self.supported_models = frozenset(supported_models)
        if max_context_tokens is not None:
            self.max_context_tokens = max_context_tokens
        self._scripted_chunks: list[str] | None = (
            list(scripted_chunks) if scripted_chunks is not None else None
        )
        if min_chunk_delay_seconds < 0:
            raise ValueError("min_chunk_delay_seconds doit être >= 0.")
        self._delay = min_chunk_delay_seconds
        self._force_fail = force_fail
        self._echo = echo_user_message
        # F2 — tool_call scripté : si fourni, le premier yield est un
        # ChatChunk portant un `tool_call` + finish_reason=TOOL_CALLS.
        # Format attendu : {"id": "call_X", "name": "create_task",
        #                   "arguments": {...}}  (arguments sérialisés en JSON).
        self._scripted_tool_call = scripted_tool_call

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncIterator[ChatChunk]:
        model = request.model or self.default_model
        if not self.supports_model(model):
            raise ProviderInvalidRequestError(
                f"Modèle '{model}' non supporté par le provider mock '{self.name}'.",
                provider=self.name,
                model=model,
            )

        if self._force_fail is not None:
            raise self._force_fail

        # F2 — Tool call scripté : yield un chunk avec tool_call puis
        # finish_reason=TOOL_CALLS (pas de texte). Le caller détecte
        # TOOL_CALLS et exécute le tool côté serveur.
        if self._scripted_tool_call is not None:
            import json as _json  # noqa: PLC0415

            tc = self._scripted_tool_call
            arguments_json = _json.dumps(tc.get("arguments", {}))
            yield ChatChunk(
                delta="",
                tool_call=ToolCallDelta(
                    id=tc.get("id", "call_mock"),
                    name=tc.get("name", ""),
                    arguments_json_partial=arguments_json,
                    index=0,
                ),
            )
            prompt_chars = sum(len(m.content) for m in request.messages)
            usage = ChatUsage(
                prompt_tokens=max(1, prompt_chars // 4),
                completion_tokens=0,
                total_tokens=max(1, prompt_chars // 4),
            )
            yield ChatChunk(
                delta="",
                finish_reason=FinishReason.TOOL_CALLS,
                usage=usage,
            )
            return

        chunks = self._scripted_chunks
        if chunks is None:
            reply = _DEFAULT_REPLY
            if self._echo and request.messages:
                last_user = next(
                    (m for m in reversed(request.messages) if m.role == "user"),
                    None,
                )
                if last_user is not None:
                    reply = f"[mock:{self.name}] tu as dit : {last_user.content[:120]}"
            chunks = _default_chunks(reply)

        total_out_chars = 0
        for chunk in chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            total_out_chars += len(chunk)
            yield ChatChunk(delta=chunk)

        prompt_chars = sum(len(m.content) for m in request.messages)
        usage = ChatUsage(
            prompt_tokens=max(1, prompt_chars // 4),
            completion_tokens=max(1, total_out_chars // 4),
            total_tokens=max(2, (prompt_chars + total_out_chars) // 4),
        )
        yield ChatChunk(delta="", finish_reason=FinishReason.STOP, usage=usage)

    async def health_check(self) -> bool:
        return True
