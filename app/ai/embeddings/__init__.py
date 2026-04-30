"""
NEXYA Couche IA — Embeddings.

Module dédié à la représentation vectorielle de texte (famille séparée
des providers chat/image car sémantiquement distincte : chat=génération,
embeddings=encodage).

Import simple :
    from app.ai.embeddings import (
        EmbeddingsProvider, EmbeddingsResponse, EmbeddingVector,
        OpenAIEmbeddingsProvider, MockEmbeddingsProvider,
        get_embeddings_provider, reset_embeddings_provider,
    )
"""

from __future__ import annotations

from app.ai.embeddings.base import (
    EmbeddingsAuthError,
    EmbeddingsError,
    EmbeddingsInvalidRequestError,
    EmbeddingsProvider,
    EmbeddingsRateLimitError,
    EmbeddingsResponse,
    EmbeddingsUnavailableError,
    EmbeddingsUsage,
    EmbeddingVector,
)
from app.ai.embeddings.gemini_embeddings import GeminiEmbeddingsProvider
from app.ai.embeddings.mock_embeddings import MockEmbeddingsProvider
from app.ai.embeddings.openai_embeddings import OpenAIEmbeddingsProvider
from app.ai.embeddings.runtime import (
    get_embeddings_provider,
    reset_embeddings_provider,
    reset_embeddings_provider_for_tests,
)

__all__ = [
    # Types neutres
    "EmbeddingVector",
    "EmbeddingsUsage",
    "EmbeddingsResponse",
    # ABC
    "EmbeddingsProvider",
    # Erreurs typées
    "EmbeddingsError",
    "EmbeddingsAuthError",
    "EmbeddingsRateLimitError",
    "EmbeddingsUnavailableError",
    "EmbeddingsInvalidRequestError",
    # Implémentations
    "OpenAIEmbeddingsProvider",
    "GeminiEmbeddingsProvider",
    "MockEmbeddingsProvider",
    # Factory
    "get_embeddings_provider",
    "reset_embeddings_provider",
    "reset_embeddings_provider_for_tests",
]
