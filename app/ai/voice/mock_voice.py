"""
MockVoiceProvider — impl déterministe sans réseau pour dev/test/CI.

- `transcribe()` retourne un texte synthétique basé sur `filename` et
  `len(audio_bytes)` — déterministe, utilisable pour tester le pipeline
  applicatif complet sans clé OpenAI.
- `synthesize()` génère un petit MP3 silencieux (header MP3 minimal).
  Déterministe par `text` pour l'isolation des tests.
- `cost_usd = 0.0` partout — le mock est gratuit.

Activé automatiquement par `get_voice_provider()` quand :
- `settings.openai_api_key` est vide.
- OU `settings.voice_mock_enabled=True` (force mock en CI).
"""

from __future__ import annotations

import hashlib
from typing import Final

import structlog

from app.ai.voice.base import (
    TranscriptionResult,
    TTSFormat,
    TTSResult,
    TTSVoice,
    VoiceInvalidRequestError,
    VoiceProvider,
)

log = structlog.get_logger()


# Heuristique MP3 128 kbps : ~16 000 bytes/seconde d'audio. Utilisée
# pour retourner une `duration_seconds` plausible à partir de la taille
# de l'audio d'entrée en mock.
_MP3_BYTES_PER_SECOND: Final[int] = 16_000


# Header MP3 minimal + silence — un frame MP3 de ~1 ms silencieux.
# Volontairement minuscule pour les tests (pas besoin de vrai audio).
_MINIMAL_MP3_FRAME: Final[bytes] = (
    b"\xff\xfb\x90\x00"  # MPEG-1 Layer III sync + header
    + b"\x00" * 32  # silence
)


class MockVoiceProvider(VoiceProvider):
    """Mock déterministe — STT texte synthétique + TTS MP3 silencieux."""

    name: Final[str] = "mock"

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        size = len(audio_bytes or b"")
        duration = round(max(0.1, size / _MP3_BYTES_PER_SECOND), 3)
        # Texte déterministe basé sur la SHA du contenu (stable par
        # audio identique — utile pour tester la dédup).
        sha = hashlib.sha256(audio_bytes or b"").hexdigest()[:16]
        text = f"[MOCK transcription sha={sha}] {size} bytes ({filename or 'unnamed'})"
        detected_lang = language or "fr"

        log.debug(
            "voice.mock.transcribe",
            size=size,
            duration_s=duration,
            filename=filename,
        )

        return TranscriptionResult(
            text=text,
            language=detected_lang,
            duration_seconds=duration,
            model="mock-whisper",
            provider=self.name,
            cost_usd=0.0,
        )

    async def synthesize(
        self,
        text: str,
        *,
        voice: TTSVoice = "alloy",
        speed: float = 1.0,
        model: str = "tts-1",
        fmt: TTSFormat = "mp3",
    ) -> TTSResult:
        if fmt != "mp3":
            # Le mock ne génère que du MP3 (c'est la forme la plus
            # couramment testée). Les autres formats raisent — même
            # sémantique que le vrai provider sur un modèle invalide.
            raise VoiceInvalidRequestError(
                f"Mock supporte uniquement fmt='mp3' (reçu '{fmt}')",
                provider="mock",
            )

        chars = len(text or "")
        # Nombre de frames = caractères / 10 (heuristique déterministe).
        # Audio MP3 silencieux mais bien formé côté header.
        n_frames = max(1, chars // 10)
        audio_bytes = _MINIMAL_MP3_FRAME * n_frames

        log.debug(
            "voice.mock.synthesize",
            chars=chars,
            voice=voice,
            audio_bytes=len(audio_bytes),
        )

        return TTSResult(
            audio_bytes=audio_bytes,
            mime_type="audio/mpeg",
            voice=voice,
            model="mock-tts",
            provider=self.name,
            chars=chars,
            cost_usd=0.0,
        )
