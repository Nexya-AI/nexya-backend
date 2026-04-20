"""
NEXYA Couche IA — Provider Anthropic (stub).

Réserve l'identité `anthropic` pour la phase suivante. Voir `openai_provider.py`
pour la motivation du pattern stub.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from .base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatProvider,
    ProviderCapability,
    ProviderUnavailableError,
)


class AnthropicChatProvider(ChatProvider):
    """Stub — implémentation réelle à venir avec la clé Anthropic."""

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

    async def stream_chat(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatChunk]:
        raise ProviderUnavailableError(
            "Anthropic provider non encore implémenté — clé API en attente.",
            provider=self.name,
            model=request.model or self.default_model,
        )
        yield  # pragma: no cover

    async def health_check(self) -> bool:
        return False
