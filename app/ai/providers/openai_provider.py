"""
NEXYA Couche IA — Provider OpenAI (stub).

Ce fichier réserve l'identité `openai` pour la phase suivante. Il implémente
le contrat `ChatProvider` mais lève systématiquement `ProviderUnavailableError`
à l'appel — comme ça le `LlmRouter` peut le déclarer dans sa config sans
crasher au démarrage, et il sera "réveillé" dès qu'Ivan branchera la clé.

Pourquoi un stub plutôt que rien ? Parce que ça documente l'intention
architecturale (OpenAI sera un provider de premier rang) et que ça permet
d'écrire les tests d'intégration du router avant même d'avoir la clé API.
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


class OpenAIChatProvider(ChatProvider):
    """Stub — implémentation réelle à venir avec la clé OpenAI."""

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

    async def stream_chat(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatChunk]:
        raise ProviderUnavailableError(
            "OpenAI provider non encore implémenté — clé API en attente.",
            provider=self.name,
            model=request.model or self.default_model,
        )
        yield  # pragma: no cover  (signe l'async generator)

    async def health_check(self) -> bool:
        return False
