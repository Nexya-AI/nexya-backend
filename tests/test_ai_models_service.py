"""N1 — Tests AiModelsService aggregation."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.ai.providers.base import ProviderCapability
from app.ai.providers.mock import MockChatProvider
from app.features.ai_models.service import (
    AiModelsService,
    _capabilities_to_strings,
    _display_name_for,
    _is_mock,
    _tier_for,
)


def test_display_name_lookup_known():
    assert _display_name_for("gemini-2.5-flash") == "Gemini 2.5 Flash"
    assert _display_name_for("gpt-4o") == "GPT-4o"
    assert _display_name_for("claude-sonnet-4-6") == "Claude Sonnet 4.6"


def test_display_name_fallback_for_unknown():
    out = _display_name_for("custom-foo-bar")
    assert "Foo Bar" in out or "Custom" in out


def test_tier_thresholds():
    assert _tier_for(8_192) == "flash"
    assert _tier_for(31_999) == "flash"
    assert _tier_for(32_768) == "pro"
    assert _tier_for(128_000) == "pro"
    assert _tier_for(999_999) == "pro"
    assert _tier_for(1_000_000) == "ultra"
    assert _tier_for(2_000_000) == "ultra"


def test_capabilities_to_strings_sorted():
    caps = frozenset(
        {
            ProviderCapability.TEXT_CHAT,
            ProviderCapability.VISION,
            ProviderCapability.FUNCTION_CALLING,
        }
    )
    out = _capabilities_to_strings(caps)
    assert out == sorted(out)
    assert "text_chat" in out
    assert "vision" in out
    assert "function_calling" in out


def test_is_mock_detection():
    real = MagicMock()
    real.__class__ = type("FakeReal", (), {})
    assert _is_mock(real) is False

    mock = MockChatProvider(name="gemini")
    assert _is_mock(mock) is True


def test_list_models_returns_response():
    """Smoke test : appelle le service en mode dev (Mock providers
    visibles), vérifie que la réponse est bien formée."""
    payload = AiModelsService.list_models()
    assert payload.models  # non vide en dev
    assert payload.experts_routing
    # Au moins l'expert "general" est présent dans le routing
    assert "general" in payload.experts_routing


def test_list_models_includes_capabilities():
    payload = AiModelsService.list_models()
    for m in payload.models:
        assert isinstance(m.capabilities, list)
        assert m.max_context_tokens >= 0
        assert m.tier in ("flash", "pro", "ultra")
        assert isinstance(m.is_default_for, list)


def test_list_models_is_default_for_general():
    """L'expert 'general' route vers gemini-2.5-flash → ce model_id
    doit avoir 'general' dans is_default_for."""
    payload = AiModelsService.list_models()
    flash_models = [
        m for m in payload.models if m.model_id == "gemini-2.5-flash" and m.provider == "gemini"
    ]
    assert flash_models, "gemini-2.5-flash absent du payload"
    assert "general" in flash_models[0].is_default_for


def test_experts_routing_dict_complete():
    """experts_routing doit contenir les 11 experts (general + 10)."""
    payload = AiModelsService.list_models()
    expected_min = {
        "general",
        "computer",
        "science",
        "finance",
        "language",
        "cooking",
        "studio",
        "engineering",
        "productivity",
        "medicine",
        "legal",
    }
    assert expected_min <= set(payload.experts_routing.keys())
