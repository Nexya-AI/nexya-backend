"""
Tests unitaires — factory `get_embeddings_provider()` (Session D1).

Valide la stratégie mock-first : Mock si clé OpenAI absente OU si flag
forcé, OpenAI sinon, singleton respecté, reset_embeddings_provider()
ré-instancie correctement.
"""

from __future__ import annotations

import pytest

from app.ai.embeddings import (
    MockEmbeddingsProvider,
    OpenAIEmbeddingsProvider,
    get_embeddings_provider,
    reset_embeddings_provider,
)


def test_factory_returns_mock_when_openai_api_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, MockEmbeddingsProvider)


def test_factory_returns_mock_when_forced_even_with_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Même avec une vraie clé, `embeddings_mock_enabled=True` force le Mock."""
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-fake-real", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", True, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, MockEmbeddingsProvider)


def test_factory_returns_openai_impl_when_key_set_and_mock_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-fake-real", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, OpenAIEmbeddingsProvider)


def test_factory_singleton_returns_same_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    a = get_embeddings_provider()
    b = get_embeddings_provider()
    assert a is b


def test_reset_reinstances_after_config_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset → on peut basculer Mock → OpenAI sans redémarrer le process."""
    from app.config import settings

    # Phase 1 : Mock.
    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    p1 = get_embeddings_provider()
    assert isinstance(p1, MockEmbeddingsProvider)

    # Phase 2 : on pose la clé, reset, factory retourne OpenAI impl.
    monkeypatch.setattr(settings, "openai_api_key", "sk-later-added", raising=False)
    reset_embeddings_provider()
    p2 = get_embeddings_provider()
    assert isinstance(p2, OpenAIEmbeddingsProvider)
    assert p2 is not p1


# ══════════════════════════════════════════════════════════════
# G1 — priorité Gemini quand OpenAI absent / override explicite
# ══════════════════════════════════════════════════════════════


def test_factory_returns_gemini_when_only_gemini_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cas d'usage G1 au 2026-04-26 : seule GEMINI_API_KEY renseignée."""
    from app.ai.embeddings import GeminiEmbeddingsProvider
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-fake", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, GeminiEmbeddingsProvider)
    assert provider.dim == 768


def test_factory_prefers_openai_when_both_keys_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-detection : OpenAI l'emporte quand les deux clés sont dispos.

    Rationnel : D1/D4 ont leur colonne `vector(1536)` figée — on ne bascule
    pas vers Gemini 768 sans override explicite (migration requise).
    """
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-real", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-real", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, OpenAIEmbeddingsProvider)


def test_factory_override_gemini_forces_gemini_even_with_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.embeddings import GeminiEmbeddingsProvider
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-real", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-real", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", False, raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "gemini", raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, GeminiEmbeddingsProvider)


def test_factory_override_mock_uses_expert_corpus_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Override `mock` → dim = `settings.expert_corpus_embedding_dim`."""
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-real", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-real", raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "mock", raising=False)
    monkeypatch.setattr(settings, "expert_corpus_embedding_dim", 768, raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, MockEmbeddingsProvider)
    assert provider.dim == 768


def test_factory_mock_dim_configurable_to_1536(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permet de rester compatible D1 (vector(1536)) côté tests mémoire."""
    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(settings, "embeddings_provider", "auto", raising=False)
    monkeypatch.setattr(settings, "embeddings_mock_enabled", True, raising=False)
    monkeypatch.setattr(settings, "expert_corpus_embedding_dim", 1536, raising=False)
    reset_embeddings_provider()
    provider = get_embeddings_provider()
    assert isinstance(provider, MockEmbeddingsProvider)
    assert provider.dim == 1536
