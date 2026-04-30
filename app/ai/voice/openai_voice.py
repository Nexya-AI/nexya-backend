"""
OpenAIVoiceProvider — impl réelle via `openai` SDK (Whisper + TTS).

Utilise `client.audio.transcriptions.create()` et `client.audio.speech.create()`.
Même SDK que `OpenAIChatProvider` (B1) et `OpenAIEmbeddingsProvider` (D1) —
même discipline `max_retries=0` (notre RetryPolicy a le contrôle
exclusif), même pattern `_map_sdk_exception` qui traduit les erreurs
SDK natives en `VoiceError` typée.

Prix tracé par row (grille OpenAI 2026-04-24) :
- Whisper-1 : **$0.006/minute** → `cost_usd = duration_seconds / 60 * 0.006`.
- TTS-1    : **$15/1M chars** → `cost_usd = len(text) * 15 / 1_000_000`.
- TTS-1-HD : **$30/1M chars** → `cost_usd = len(text) * 30 / 1_000_000`.
"""

from __future__ import annotations

import io
from typing import Final

import structlog

from app.ai.voice.base import (
    TranscriptionResult,
    TTSFormat,
    TTSResult,
    TTSVoice,
    VoiceAuthError,
    VoiceInvalidRequestError,
    VoiceProvider,
    VoiceRateLimitError,
    VoiceUnavailableError,
)

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Grille de prix OpenAI (2026-04-24)
# ══════════════════════════════════════════════════════════════

_WHISPER_PRICE_USD_PER_MINUTE: Final[float] = 0.006

_TTS_PRICES_USD_PER_1M_CHARS: Final[dict[str, float]] = {
    "tts-1": 15.0,
    "tts-1-hd": 30.0,
}

# Mimetypes par format TTS.
_TTS_MIMETYPES: Final[dict[str, str]] = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
}


# ══════════════════════════════════════════════════════════════
# Client singleton lazy — identique pattern B1 / D1
# ══════════════════════════════════════════════════════════════

_client: object | None = None


def _get_client():
    """Retourne un `AsyncOpenAI` singleton process-wide.

    `max_retries=0` car notre `RetryPolicy` applicative a le contrôle
    exclusif — éviter les retries SDK qui doubleraient la facture.
    Timeout 120 s car Whisper peut mettre 30-60 s sur un audio long.
    """
    global _client
    if _client is not None:
        return _client

    import openai  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415

    if not settings.openai_api_key:
        raise VoiceAuthError("OPENAI_API_KEY absente", provider="openai")
    _client = openai.AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=120.0,
        max_retries=0,
    )
    return _client


def _reset_client_for_tests() -> None:
    """Reset du singleton — usage tests uniquement."""
    global _client
    _client = None


# ══════════════════════════════════════════════════════════════
# Mapping erreurs SDK → VoiceError typée
# ══════════════════════════════════════════════════════════════


def _map_sdk_exception(exc: Exception, *, model: str) -> Exception:
    """Traduit une exception `openai.*Error` en `VoiceError` typée.

    Miroir strict du pattern `OpenAIChatProvider._map_sdk_exception`
    (B1). Permet au caller d'uniformiser le handling HTTP (401/429/503/400).
    """
    import openai  # noqa: PLC0415

    if isinstance(exc, openai.AuthenticationError):
        return VoiceAuthError(str(exc), provider="openai")
    if isinstance(exc, openai.PermissionDeniedError):
        return VoiceAuthError(str(exc), provider="openai")
    if isinstance(exc, openai.RateLimitError):
        retry_after_raw = None
        headers = getattr(exc.response, "headers", None) if hasattr(exc, "response") else None
        if headers is not None:
            try:
                retry_after_raw = headers.get("retry-after")
            except Exception:  # noqa: BLE001
                retry_after_raw = None
        retry_after: float | None = None
        if retry_after_raw:
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = None
        return VoiceRateLimitError(str(exc), provider="openai", retry_after=retry_after)
    if isinstance(exc, openai.NotFoundError):
        return VoiceInvalidRequestError(
            f"Modèle '{model}' introuvable côté OpenAI",
            provider="openai",
        )
    if isinstance(exc, openai.BadRequestError):
        return VoiceInvalidRequestError(str(exc), provider="openai")
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return VoiceUnavailableError(str(exc), provider="openai")
    # Default : provider down.
    return VoiceUnavailableError(str(exc), provider="openai")


# ══════════════════════════════════════════════════════════════
# OpenAIVoiceProvider
# ══════════════════════════════════════════════════════════════


class OpenAIVoiceProvider(VoiceProvider):
    """Impl réelle via `openai` SDK (Whisper + TTS)."""

    name: Final[str] = "openai"

    def __init__(
        self,
        *,
        default_stt_model: str = "whisper-1",
        default_tts_model: str = "tts-1",
    ) -> None:
        self._default_stt_model = default_stt_model
        self._default_tts_model = default_tts_model

    # ── STT (Whisper) ──────────────────────────────────────────
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        client = _get_client()
        # Whisper API exige un file-like avec un nom (utilisé pour deviner
        # le format) — on fournit (name, content, mime) via un tuple.
        file_tuple = (filename or "audio.mp3", audio_bytes, mime_type)

        # `verbose_json` pour récupérer la `duration` précise retournée
        # par l'API (indispensable pour le tracking coût post-appel).
        kwargs: dict = {
            "file": file_tuple,
            "model": self._default_stt_model,
            "response_format": "verbose_json",
        }
        if language:
            kwargs["language"] = language

        try:
            response = await client.audio.transcriptions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=self._default_stt_model) from exc

        # Extraction des champs — le SDK retourne un objet pydantic.
        text = getattr(response, "text", "") or ""
        duration = float(getattr(response, "duration", 0.0) or 0.0)
        detected_lang = getattr(response, "language", None)

        cost_usd = round(duration / 60.0 * _WHISPER_PRICE_USD_PER_MINUTE, 6)

        log.info(
            "voice.openai.transcribe_ok",
            model=self._default_stt_model,
            duration_s=duration,
            cost_usd=cost_usd,
            detected_lang=detected_lang,
        )

        return TranscriptionResult(
            text=text,
            language=detected_lang,
            duration_seconds=duration,
            model=self._default_stt_model,
            provider=self.name,
            cost_usd=cost_usd,
        )

    # ── TTS ────────────────────────────────────────────────────
    async def synthesize(
        self,
        text: str,
        *,
        voice: TTSVoice = "alloy",
        speed: float = 1.0,
        model: str = "tts-1",
        fmt: TTSFormat = "mp3",
    ) -> TTSResult:
        if model not in _TTS_PRICES_USD_PER_1M_CHARS:
            raise VoiceInvalidRequestError(f"Modèle TTS '{model}' non supporté", provider="openai")
        if fmt not in _TTS_MIMETYPES:
            raise VoiceInvalidRequestError(f"Format '{fmt}' non supporté", provider="openai")

        client = _get_client()
        try:
            response = await client.audio.speech.create(
                model=model,
                voice=voice,
                input=text,
                speed=speed,
                response_format=fmt,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        # `response` est un `BinaryAPIResponse` — on lit les bytes.
        buffer = io.BytesIO()
        async for chunk in response.iter_bytes():
            buffer.write(chunk)
        audio_bytes = buffer.getvalue()

        chars = len(text)
        price_per_1m = _TTS_PRICES_USD_PER_1M_CHARS[model]
        cost_usd = round(chars * price_per_1m / 1_000_000, 6)

        log.info(
            "voice.openai.synthesize_ok",
            model=model,
            voice=voice,
            chars=chars,
            audio_bytes=len(audio_bytes),
            cost_usd=cost_usd,
        )

        return TTSResult(
            audio_bytes=audio_bytes,
            mime_type=_TTS_MIMETYPES[fmt],
            voice=voice,
            model=model,
            provider=self.name,
            chars=chars,
            cost_usd=cost_usd,
        )
