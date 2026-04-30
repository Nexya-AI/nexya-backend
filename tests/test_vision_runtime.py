"""Tests unitaires — factory `get_vision_provider(tier)` (E2)."""

from __future__ import annotations

import pytest

from app.ai.vision.gemini_vision import GeminiVisionProvider
from app.ai.vision.mock_vision import MockVisionProvider
from app.ai.vision.openai_vision import OpenAIVisionProvider
from app.ai.vision.runtime import (
    get_vision_provider,
    reset_vision_provider,
)


@pytest.fixture(autouse=True)
def _reset_before_each():
    reset_vision_provider()
    yield
    reset_vision_provider()


def test_factory_returns_mock_when_gemini_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "vision_mock_enabled", False, raising=False)
    p = get_vision_provider("flash")
    assert isinstance(p, MockVisionProvider)


def test_factory_returns_mock_when_flag_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "real", raising=False)
    monkeypatch.setattr(settings, "vision_mock_enabled", True, raising=False)
    p = get_vision_provider("pro")
    assert isinstance(p, MockVisionProvider)


def test_factory_flash_returns_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "real", raising=False)
    monkeypatch.setattr(settings, "vision_mock_enabled", False, raising=False)
    p = get_vision_provider("flash")
    assert isinstance(p, GeminiVisionProvider)


def test_factory_pro_defaults_to_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "real", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-x", raising=False)
    monkeypatch.setattr(settings, "vision_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "vision_pro_provider", "gemini", raising=False)
    p = get_vision_provider("pro")
    assert isinstance(p, GeminiVisionProvider)


def test_factory_pro_uses_openai_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "real", raising=False)
    monkeypatch.setattr(settings, "openai_api_key", "sk-real", raising=False)
    monkeypatch.setattr(settings, "vision_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "vision_pro_provider", "openai", raising=False)
    p = get_vision_provider("pro")
    assert isinstance(p, OpenAIVisionProvider)
