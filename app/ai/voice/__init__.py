"""Couche IA — Voice (Whisper STT + OpenAI TTS).

Pattern identique à `app/ai/embeddings/` (D1) et `app/ai/providers/` (B1) :
ABC `VoiceProvider` abstrait du SDK, 2 implémentations (OpenAI + Mock),
factory singleton mock-first automatique.

Le jour où Ivan switche vers faster-whisper self-hosted (GPU Hetzner),
on écrit une seule classe `FasterWhisperVoiceProvider` conforme à
l'ABC et on change une ligne dans la factory. Zéro réécriture applicative.
"""

from app.ai.voice.base import (
    TranscriptionResult,
    TTSFormat,
    TTSResult,
    TTSVoice,
    VoiceAuthError,
    VoiceError,
    VoiceInvalidRequestError,
    VoiceProvider,
    VoiceRateLimitError,
    VoiceUnavailableError,
)
from app.ai.voice.mock_voice import MockVoiceProvider
from app.ai.voice.openai_voice import OpenAIVoiceProvider
from app.ai.voice.runtime import (
    get_voice_provider,
    reset_voice_provider,
)

__all__ = [
    "TranscriptionResult",
    "TTSFormat",
    "TTSResult",
    "TTSVoice",
    "VoiceAuthError",
    "VoiceError",
    "VoiceInvalidRequestError",
    "VoiceProvider",
    "VoiceRateLimitError",
    "VoiceUnavailableError",
    "MockVoiceProvider",
    "OpenAIVoiceProvider",
    "get_voice_provider",
    "reset_voice_provider",
]
