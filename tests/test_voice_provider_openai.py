"""
Tests unitaires — `OpenAIVoiceProvider` avec SDK openai mocké.

On fake `openai.AsyncOpenAI` + ses méthodes `.audio.transcriptions.create`
et `.audio.speech.create` pour valider le mapping request/response +
la traduction des erreurs SDK vers `VoiceError` hiérarchie.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.voice import openai_voice as ov_module
from app.ai.voice.base import (
    VoiceAuthError,
    VoiceInvalidRequestError,
    VoiceRateLimitError,
    VoiceUnavailableError,
)
from app.ai.voice.openai_voice import OpenAIVoiceProvider

# ══════════════════════════════════════════════════════════════
# Fakes SDK
# ══════════════════════════════════════════════════════════════


class _FakeOpenAIErrors:
    """Substitut minimal du module `openai` pour isoler les tests."""

    class AuthenticationError(Exception):
        pass

    class PermissionDeniedError(Exception):
        pass

    class RateLimitError(Exception):
        def __init__(self, msg: str, *, retry_after: str | None = None) -> None:
            super().__init__(msg)
            self.response = MagicMock()
            self.response.headers = {"retry-after": retry_after} if retry_after else {}

    class NotFoundError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(Exception):
        pass


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, **kwargs) -> None:
    """Installe un faux module `openai` avec AsyncOpenAI paramétrable."""
    import sys

    fake = MagicMock()
    # Exceptions sur le module.
    fake.AuthenticationError = _FakeOpenAIErrors.AuthenticationError
    fake.PermissionDeniedError = _FakeOpenAIErrors.PermissionDeniedError
    fake.RateLimitError = _FakeOpenAIErrors.RateLimitError
    fake.NotFoundError = _FakeOpenAIErrors.NotFoundError
    fake.BadRequestError = _FakeOpenAIErrors.BadRequestError
    fake.APIConnectionError = _FakeOpenAIErrors.APIConnectionError
    fake.APITimeoutError = _FakeOpenAIErrors.APITimeoutError

    # Client mock avec audio.transcriptions.create + audio.speech.create.
    client = MagicMock()
    client.audio = MagicMock()
    client.audio.transcriptions = MagicMock()
    client.audio.transcriptions.create = kwargs.get("transcribe_create", AsyncMock())
    client.audio.speech = MagicMock()
    client.audio.speech.create = kwargs.get("speech_create", AsyncMock())
    fake.AsyncOpenAI = MagicMock(return_value=client)

    monkeypatch.setitem(sys.modules, "openai", fake)
    # Reset le singleton client.
    ov_module._reset_client_for_tests()
    # Fake clé présente pour passer le garde VoiceAuthError.
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-fake", raising=False)
    return client


# ══════════════════════════════════════════════════════════════
# 1. transcribe happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_transcribe_returns_text_duration_and_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_response = MagicMock()
    fake_response.text = "Bonjour, ceci est un test."
    fake_response.duration = 12.5
    fake_response.language = "fr"

    client = _install_fake_openai(
        monkeypatch,
        transcribe_create=AsyncMock(return_value=fake_response),
    )

    provider = OpenAIVoiceProvider()
    result = await provider.transcribe(
        b"fake audio",
        filename="test.mp3",
        mime_type="audio/mpeg",
    )

    assert result.text == "Bonjour, ceci est un test."
    assert result.duration_seconds == 12.5
    assert result.language == "fr"
    assert result.model == "whisper-1"
    assert result.provider == "openai"
    # Cost = 12.5s / 60 * 0.006 = 0.00125.
    assert result.cost_usd == round(12.5 / 60 * 0.006, 6)
    client.audio.transcriptions.create.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 2. transcribe forward language param
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_transcribe_forwards_language_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_response = MagicMock()
    fake_response.text = "hello"
    fake_response.duration = 1.0
    fake_response.language = "en"
    client = _install_fake_openai(
        monkeypatch,
        transcribe_create=AsyncMock(return_value=fake_response),
    )
    provider = OpenAIVoiceProvider()
    await provider.transcribe(
        b"x",
        filename="a.wav",
        mime_type="audio/wav",
        language="en",
    )
    kwargs = client.audio.transcriptions.create.await_args.kwargs
    assert kwargs["language"] == "en"


# ══════════════════════════════════════════════════════════════
# 3. transcribe error mapping
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_transcribe_maps_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(
        monkeypatch,
        transcribe_create=AsyncMock(side_effect=_FakeOpenAIErrors.AuthenticationError("401")),
    )
    provider = OpenAIVoiceProvider()
    with pytest.raises(VoiceAuthError):
        await provider.transcribe(b"x", filename="a.mp3", mime_type="audio/mpeg")


@pytest.mark.asyncio
async def test_openai_transcribe_maps_rate_limit_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(
        monkeypatch,
        transcribe_create=AsyncMock(
            side_effect=_FakeOpenAIErrors.RateLimitError("429", retry_after="30")
        ),
    )
    provider = OpenAIVoiceProvider()
    with pytest.raises(VoiceRateLimitError) as ctx:
        await provider.transcribe(b"x", filename="a.mp3", mime_type="audio/mpeg")
    assert ctx.value.retry_after == 30.0


@pytest.mark.asyncio
async def test_openai_transcribe_maps_connection_error_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(
        monkeypatch,
        transcribe_create=AsyncMock(
            side_effect=_FakeOpenAIErrors.APIConnectionError("network down")
        ),
    )
    provider = OpenAIVoiceProvider()
    with pytest.raises(VoiceUnavailableError):
        await provider.transcribe(b"x", filename="a.mp3", mime_type="audio/mpeg")


# ══════════════════════════════════════════════════════════════
# 4. synthesize happy path + cost TTS-1
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_synthesize_returns_audio_with_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # response.iter_bytes() est un async iterator sur les bytes.
    response = MagicMock()

    async def _iter_bytes(*args, **kwargs):
        yield b"fake-mp3-chunk-1"
        yield b"fake-mp3-chunk-2"

    response.iter_bytes = _iter_bytes
    client = _install_fake_openai(
        monkeypatch,
        speech_create=AsyncMock(return_value=response),
    )

    provider = OpenAIVoiceProvider()
    result = await provider.synthesize("hello world", model="tts-1")
    assert result.audio_bytes == b"fake-mp3-chunk-1fake-mp3-chunk-2"
    assert result.model == "tts-1"
    assert result.provider == "openai"
    assert result.chars == 11
    # Cost = 11 * 15 / 1M.
    assert result.cost_usd == round(11 * 15 / 1_000_000, 6)


# ══════════════════════════════════════════════════════════════
# 5. synthesize rejette modèle inconnu
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openai_synthesize_rejects_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(monkeypatch)
    provider = OpenAIVoiceProvider()
    with pytest.raises(VoiceInvalidRequestError):
        await provider.synthesize("hi", model="unknown-model")  # type: ignore[arg-type]
