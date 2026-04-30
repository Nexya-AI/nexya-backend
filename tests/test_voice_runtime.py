"""
Tests unitaires — factory `get_voice_provider()` (E1).

Vérifie la bascule mock-first automatique selon la config + singleton.
"""

from __future__ import annotations

import pytest

from app.ai.voice.mock_voice import MockVoiceProvider
from app.ai.voice.openai_voice import OpenAIVoiceProvider
from app.ai.voice.runtime import (
    get_voice_provider,
    reset_voice_provider,
)


@pytest.fixture(autouse=True)
def _reset_before_each():
    """Chaque test commence avec un singleton vierge."""
    reset_voice_provider()
    yield
    reset_voice_provider()


def test_factory_returns_mock_when_openai_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "voice_mock_enabled", False, raising=False)
    provider = get_voice_provider()
    assert isinstance(provider, MockVoiceProvider)


def test_factory_returns_mock_when_force_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-real", raising=False)
    monkeypatch.setattr(settings, "voice_mock_enabled", True, raising=False)
    provider = get_voice_provider()
    assert isinstance(provider, MockVoiceProvider)


def test_factory_returns_openai_when_key_present_and_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-real", raising=False)
    monkeypatch.setattr(settings, "voice_mock_enabled", False, raising=False)
    provider = get_voice_provider()
    assert isinstance(provider, OpenAIVoiceProvider)


def test_factory_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    p1 = get_voice_provider()
    p2 = get_voice_provider()
    assert p1 is p2
