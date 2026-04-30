"""Couche IA — Vision multimodale (Gemini + GPT-4o + Claude).

Pattern miroir `app/ai/voice/` (E1) et `app/ai/embeddings/` (D1) :
ABC `VisionProvider` abstraite du SDK, 3 implémentations (Gemini / OpenAI /
Mock), factory singleton mock-first automatique par tier.

Portabilité stratégique garantie : le jour où Gemini devient trop cher ou
qu'un modèle alternatif (Pixtral, Qwen-VL) supplante, il suffit d'écrire
1 classe `XxxVisionProvider` conforme à l'ABC.
"""

from app.ai.vision.base import (
    ImageInput,
    VisionAuthError,
    VisionContentFilteredError,
    VisionError,
    VisionInvalidRequestError,
    VisionProvider,
    VisionRateLimitError,
    VisionResult,
    VisionTier,
    VisionUnavailableError,
)
from app.ai.vision.gemini_vision import GeminiVisionProvider
from app.ai.vision.mock_vision import MockVisionProvider
from app.ai.vision.openai_vision import OpenAIVisionProvider
from app.ai.vision.runtime import (
    get_vision_provider,
    reset_vision_provider,
)

__all__ = [
    "ImageInput",
    "VisionAuthError",
    "VisionContentFilteredError",
    "VisionError",
    "VisionInvalidRequestError",
    "VisionProvider",
    "VisionRateLimitError",
    "VisionResult",
    "VisionTier",
    "VisionUnavailableError",
    "GeminiVisionProvider",
    "MockVisionProvider",
    "OpenAIVisionProvider",
    "get_vision_provider",
    "reset_vision_provider",
]
