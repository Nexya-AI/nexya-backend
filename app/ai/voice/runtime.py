"""
Factory singleton `VoiceProvider` — mock-first automatique.

Aligné pattern `app/ai/embeddings/runtime.py` (D1) et
`app/core/storage/object_store.py::get_object_store` (C3) :

- 1er appel : décide du backend selon la config actuelle.
- Cache l'instance pour la durée du process (économie cold-start).
- `reset_voice_provider()` pour les tests (re-choix après monkey-patch
  de `settings.openai_api_key` ou `settings.voice_mock_enabled`).

Stratégie mock-first :

    settings.voice_mock_enabled = True   → MockVoiceProvider forcé (CI)
    settings.openai_api_key = ""         → MockVoiceProvider auto
    sinon                                → OpenAIVoiceProvider

Le jour où Ivan provisionne un GPU Hetzner et installe `faster-whisper`,
on ajoute une branche :

    elif settings.voice_provider == "faster-whisper":
        _PROVIDER = FasterWhisperVoiceProvider(...)

Zéro changement dans le reste du code — tout passe par `VoiceProvider`.
"""

from __future__ import annotations

import structlog

from app.ai.voice.base import VoiceProvider
from app.ai.voice.mock_voice import MockVoiceProvider
from app.ai.voice.openai_voice import OpenAIVoiceProvider

log = structlog.get_logger()


_PROVIDER: VoiceProvider | None = None


def get_voice_provider() -> VoiceProvider:
    """Retourne le singleton VoiceProvider selon la config.

    - Mock si `settings.voice_mock_enabled=True`.
    - Mock si `settings.openai_api_key=""` (clé non fournie).
    - OpenAI sinon.
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER

    from app.config import settings  # noqa: PLC0415 — évite import circulaire startup

    if settings.voice_mock_enabled or not settings.openai_api_key:
        _PROVIDER = MockVoiceProvider()
        log.info(
            "voice.provider.initialized",
            name=_PROVIDER.name,
            reason=("mock_enabled" if settings.voice_mock_enabled else "no_api_key"),
        )
        return _PROVIDER

    _PROVIDER = OpenAIVoiceProvider(
        default_stt_model=settings.voice_default_stt_model,
        default_tts_model=settings.voice_default_tts_model,
    )
    log.info(
        "voice.provider.initialized",
        name=_PROVIDER.name,
        stt_model=settings.voice_default_stt_model,
        tts_model=settings.voice_default_tts_model,
    )
    return _PROVIDER


def reset_voice_provider() -> None:
    """Réinitialise le singleton — usage tests uniquement."""
    global _PROVIDER
    _PROVIDER = None
