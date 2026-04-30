"""
Tests — token_estimator (brique B2).

Couverture :
1. `estimate` renvoie un `TokenEstimate` cohérent (prompt + completion + cost).
2. Heuristique caractères → tokens : majoration +15 %, structural overhead inclus.
3. `estimate_completion_budget` : défaut 1 024, max_tokens explicite respecté.
4. Provider inconnu (Anthropic, Gemini) retombe proprement sur l'heuristique.
5. `tiktoken` absent ou crash → fallback heuristique + warning unique.

Discipline : tests offline, pas de download de BPE — on simule le comportement
via monkeypatch sur `_load_tiktoken_encoder`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.ai import token_estimator as te_module
from app.ai.providers.base import ChatMessage
from app.ai.token_estimator import (
    TokenEstimate,
    _reset_tiktoken_cache_for_tests,
    estimate,
    estimate_completion_budget,
    estimate_prompt_tokens,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    _reset_tiktoken_cache_for_tests()
    yield
    _reset_tiktoken_cache_for_tests()


# ══════════════════════════════════════════════════════════════
# estimate — API principale
# ══════════════════════════════════════════════════════════════


def test_estimate_returns_tokenestimate_with_all_fields():
    result = estimate(
        provider="gemini",
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="Bonjour NEXYA")],
        system_prompt="Tu es NEXYA.",
        max_tokens=500,
    )
    assert isinstance(result, TokenEstimate)
    assert result.provider == "gemini"
    assert result.model == "gemini-2.5-flash"
    assert result.prompt_tokens > 0
    assert result.max_completion_tokens == 500
    assert result.estimated_total_tokens == result.prompt_tokens + 500


def test_estimate_with_default_completion():
    result = estimate(
        provider="gemini",
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="Hi")],
    )
    assert result.max_completion_tokens == 1024


def test_estimate_cost_usd_is_non_negative():
    result = estimate(
        provider="openai",
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="Hello")],
        max_tokens=100,
    )
    # Avec un modèle tarifé, le coût worst-case est positif.
    assert result.estimated_cost_usd >= 0


# ══════════════════════════════════════════════════════════════
# estimate_completion_budget
# ══════════════════════════════════════════════════════════════


def test_completion_budget_returns_default_when_none():
    assert estimate_completion_budget(requested_max_tokens=None) == 1024


def test_completion_budget_returns_default_when_zero():
    assert estimate_completion_budget(requested_max_tokens=0) == 1024


def test_completion_budget_returns_default_when_negative():
    assert estimate_completion_budget(requested_max_tokens=-10) == 1024


def test_completion_budget_respects_explicit_value():
    assert estimate_completion_budget(requested_max_tokens=2048) == 2048


# ══════════════════════════════════════════════════════════════
# estimate_prompt_tokens — heuristique (Gemini, Anthropic — sans tiktoken)
# ══════════════════════════════════════════════════════════════


def test_heuristic_for_gemini_provider():
    """Gemini n'est pas dans `_TIKTOKEN_ENCODER_BY_PROVIDER` → heuristique directe."""
    prompt_tokens = estimate_prompt_tokens(
        provider="gemini",
        model="gemini-2.5-flash",
        messages=[ChatMessage(role="user", content="x" * 300)],  # 300 chars
    )
    # 300 chars / 3.0 = 100 tokens × 1.15 = 115 + overhead ~6 → ~121
    assert 100 <= prompt_tokens <= 150


def test_heuristic_for_anthropic_provider():
    prompt_tokens = estimate_prompt_tokens(
        provider="anthropic",
        model="claude-sonnet-4-6",
        messages=[ChatMessage(role="user", content="y" * 600)],
    )
    # 600 / 3.0 × 1.15 ≈ 230 + overhead
    assert 220 <= prompt_tokens <= 260


def test_heuristic_includes_system_prompt_in_count():
    without = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[ChatMessage(role="user", content="Hello")],
    )
    with_sys = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[ChatMessage(role="user", content="Hello")],
        system_prompt="Tu es NEXYA, assistant de Nexyalabs.",
    )
    assert with_sys > without


def test_heuristic_structural_overhead_proportional_to_messages():
    single = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[ChatMessage(role="user", content="Hi")],
    )
    multi = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hi"),
            ChatMessage(role="user", content="Hi"),
        ],
    )
    # Plus de messages → plus d'overhead structurel
    assert multi > single


# ══════════════════════════════════════════════════════════════
# estimate_prompt_tokens — tiktoken (OpenAI, Qwen)
# ══════════════════════════════════════════════════════════════


def test_tiktoken_path_for_openai_when_available(monkeypatch):
    """Si tiktoken est disponible, OpenAI passe par l'encoder."""
    fake_encoder = MagicMock()
    fake_encoder.encode = MagicMock(return_value=[0, 1, 2, 3, 4])  # 5 tokens par texte
    monkeypatch.setattr(te_module, "_load_tiktoken_encoder", lambda name: fake_encoder)
    prompt_tokens = estimate_prompt_tokens(
        provider="openai",
        model="gpt-4o",
        messages=[
            ChatMessage(role="user", content="a"),
            ChatMessage(role="assistant", content="b"),
        ],
    )
    # 2 textes × 5 tokens + 2 messages × 3 overhead + 3 primer = 19
    assert prompt_tokens == 19


def test_tiktoken_fallback_to_heuristic_when_not_installed(monkeypatch):
    """Si tiktoken est absent → heuristique, pas de crash."""
    monkeypatch.setattr(te_module, "_load_tiktoken_encoder", lambda name: None)
    prompt_tokens = estimate_prompt_tokens(
        provider="openai",
        model="gpt-4o",
        messages=[ChatMessage(role="user", content="Hello world")],
    )
    # Heuristique ~4 tokens, on vérifie juste que c'est > 0 et pas délirant
    assert 0 < prompt_tokens < 100


def test_tiktoken_fallback_to_heuristic_when_encoder_raises(monkeypatch):
    """Si `encoder.encode` crash, on bascule sur l'heuristique sans propager."""
    fake_encoder = MagicMock()
    fake_encoder.encode = MagicMock(side_effect=RuntimeError("BPE corrupted"))
    monkeypatch.setattr(te_module, "_load_tiktoken_encoder", lambda name: fake_encoder)
    prompt_tokens = estimate_prompt_tokens(
        provider="openai",
        model="gpt-4o",
        messages=[ChatMessage(role="user", content="Hello")],
    )
    assert prompt_tokens > 0  # heuristique appelée


# ══════════════════════════════════════════════════════════════
# Invariants
# ══════════════════════════════════════════════════════════════


def test_longer_prompt_yields_more_tokens():
    short = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[ChatMessage(role="user", content="Hi")],
    )
    long_msg = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[ChatMessage(role="user", content="Hello, NEXYA!" * 50)],
    )
    assert long_msg > short


def test_empty_messages_yields_only_structural_overhead():
    """Aucun message → juste l'overhead + primer."""
    prompt_tokens = estimate_prompt_tokens(
        provider="gemini",
        model="m",
        messages=[],
    )
    # 0 texte + 0 messages × 3 + 3 primer = 3
    assert prompt_tokens == 3
