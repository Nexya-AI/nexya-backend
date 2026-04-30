"""
Tests unitaires — `MockVoiceProvider` (E1).

Déterminisme strict sur transcribe + synthesize. Permet à tous les tests
downstream (service + router) de tourner sans clé OpenAI.
"""

from __future__ import annotations

import hashlib

import pytest

from app.ai.voice.base import VoiceInvalidRequestError
from app.ai.voice.mock_voice import MockVoiceProvider

# ══════════════════════════════════════════════════════════════
# 1. transcribe — texte déterministe basé sur SHA
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_transcribe_returns_deterministic_text_per_input() -> None:
    provider = MockVoiceProvider()
    data = b"fake-audio-1234"
    r1 = await provider.transcribe(data, filename="test.mp3", mime_type="audio/mpeg")
    r2 = await provider.transcribe(data, filename="test.mp3", mime_type="audio/mpeg")
    # Déterministe : même input → même texte.
    assert r1.text == r2.text
    # Texte contient la SHA[:16] du contenu.
    sha_short = hashlib.sha256(data).hexdigest()[:16]
    assert sha_short in r1.text


@pytest.mark.asyncio
async def test_mock_transcribe_duration_scales_with_size() -> None:
    provider = MockVoiceProvider()
    small = await provider.transcribe(b"x" * 1000, filename="a.mp3", mime_type="audio/mpeg")
    big = await provider.transcribe(b"x" * 32_000, filename="b.mp3", mime_type="audio/mpeg")
    assert big.duration_seconds > small.duration_seconds


@pytest.mark.asyncio
async def test_mock_transcribe_cost_is_zero() -> None:
    provider = MockVoiceProvider()
    r = await provider.transcribe(b"test", filename="x.mp3", mime_type="audio/mpeg")
    assert r.cost_usd == 0.0
    assert r.provider == "mock"
    assert r.model == "mock-whisper"


# ══════════════════════════════════════════════════════════════
# 2. synthesize — MP3 silencieux déterministe
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_synthesize_returns_mp3_bytes_deterministic() -> None:
    provider = MockVoiceProvider()
    r1 = await provider.synthesize("hello world")
    r2 = await provider.synthesize("hello world")
    # Déterministe.
    assert r1.audio_bytes == r2.audio_bytes
    assert r1.mime_type == "audio/mpeg"
    assert r1.cost_usd == 0.0
    assert r1.chars == len("hello world")


@pytest.mark.asyncio
async def test_mock_synthesize_rejects_non_mp3_format() -> None:
    provider = MockVoiceProvider()
    with pytest.raises(VoiceInvalidRequestError):
        await provider.synthesize("hi", fmt="opus")
