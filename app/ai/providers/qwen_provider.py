"""
NEXYA Couche IA — Provider Qwen / Alibaba (stub).

Réserve l'identité `qwen` pour la phase suivante. Qwen 2.5 est notre candidat
pour les langues africaines (meilleur que Gemma selon benchmarks 2026) et
sera notre cheval de bataille pour migrer vers l'open-source managé.

Voir `openai_provider.py` pour la motivation du pattern stub.
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


class QwenChatProvider(ChatProvider):
    """Stub — implémentation réelle à venir avec la clé DashScope."""

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

    async def stream_chat(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatChunk]:
        raise ProviderUnavailableError(
            "Qwen provider non encore implémenté — clé DashScope en attente.",
            provider=self.name,
            model=request.model or self.default_model,
        )
        yield  # pragma: no cover

    async def health_check(self) -> bool:
        return False
