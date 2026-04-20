"""
NEXYA Couche IA — Registre des providers.

Import simple depuis ce paquet :
    from app.ai.providers import ChatProvider, ChatCompletionRequest, GeminiChatProvider
"""

from __future__ import annotations

from .anthropic_provider import AnthropicChatProvider
from .base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatMessage,
    ChatProvider,
    ChatRole,
    ChatUsage,
    FinishReason,
    GeneratedImage,
    ImageGenerationRequest,
    ImageProvider,
    ProviderAuthError,
    ProviderCapability,
    ProviderContentFilteredError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from .gemini import GeminiChatProvider, GeminiImageProvider
from .openai_provider import OpenAIChatProvider
from .qwen_provider import QwenChatProvider

__all__ = [
    # Types neutres
    "ChatMessage",
    "ChatRole",
    "ChatUsage",
    "ChatChunk",
    "FinishReason",
    "ChatCompletionRequest",
    "ImageGenerationRequest",
    "GeneratedImage",
    # Interfaces abstraites
    "ChatProvider",
    "ImageProvider",
    "ProviderCapability",
    # Erreurs
    "ProviderError",
    "ProviderUnavailableError",
    "ProviderRateLimitError",
    "ProviderAuthError",
    "ProviderContentFilteredError",
    "ProviderInvalidRequestError",
    # Implémentations
    "GeminiChatProvider",
    "GeminiImageProvider",
    "OpenAIChatProvider",
    "AnthropicChatProvider",
    "QwenChatProvider",
]
