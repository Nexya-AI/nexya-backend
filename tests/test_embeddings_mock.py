"""
Tests unitaires — `MockEmbeddingsProvider` (Session D1).

Valide le contrat : dim 1536, norm L2 = 1.0, déterminisme, batching,
propriétés du provider, estimation usage.
"""

from __future__ import annotations

import math

import pytest

from app.ai.embeddings import (
    EmbeddingsInvalidRequestError,
    EmbeddingsResponse,
    MockEmbeddingsProvider,
)

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _l2_norm(values: list[float]) -> float:
    return math.sqrt(sum(x * x for x in values))


# ══════════════════════════════════════════════════════════════
# 1. Propriétés du provider
# ══════════════════════════════════════════════════════════════


def test_mock_provider_has_expected_properties() -> None:
    provider = MockEmbeddingsProvider()
    assert provider.name == "mock"
    assert provider.default_model == "mock-1536"
    assert provider.dim == 1536


# ══════════════════════════════════════════════════════════════
# 2. Vecteurs — dimension + normalisation L2
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_vector_has_exact_1536_dim() -> None:
    provider = MockEmbeddingsProvider()
    response = await provider.embed(["Bonjour NEXYA"])
    assert isinstance(response, EmbeddingsResponse)
    assert len(response.vectors) == 1
    vec = response.vectors[0]
    assert len(vec.values) == 1536
    assert vec.dim == 1536


@pytest.mark.asyncio
async def test_mock_vector_is_l2_normalized() -> None:
    provider = MockEmbeddingsProvider()
    response = await provider.embed(["Texte quelconque pour vérifier la norme."])
    norm = _l2_norm(response.vectors[0].values)
    # Norm L2 strictement proche de 1.0 (± 1e-6 pour tolérance float).
    assert abs(norm - 1.0) < 1e-6


# ══════════════════════════════════════════════════════════════
# 3. Déterminisme
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_same_text_same_vector() -> None:
    """Même texte → même vecteur, rejouable à l'identique."""
    provider = MockEmbeddingsProvider()
    text = "Ivan est développeur Flutter"
    r1 = await provider.embed([text])
    r2 = await provider.embed([text])
    assert r1.vectors[0].values == r2.vectors[0].values


@pytest.mark.asyncio
async def test_mock_different_texts_different_vectors() -> None:
    """Deux textes distincts → vecteurs distincts (même si le Mock
    n'a pas de sémantique, la divergence SHA garantit la différence)."""
    provider = MockEmbeddingsProvider()
    r = await provider.embed(
        [
            "Ivan est développeur Flutter",
            "J'aime la cuisine camerounaise",
        ]
    )
    assert r.vectors[0].values != r.vectors[1].values


# ══════════════════════════════════════════════════════════════
# 4. Batching — N textes → N vecteurs dans l'ordre
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_batch_returns_N_vectors_in_order() -> None:
    provider = MockEmbeddingsProvider()
    texts = [f"memoire-{i}" for i in range(10)]
    response = await provider.embed(texts)
    assert len(response.vectors) == 10

    # Rejouer texte par texte doit donner le même vecteur que dans le batch.
    for idx, text in enumerate(texts):
        single = await provider.embed([text])
        assert single.vectors[0].values == response.vectors[idx].values


# ══════════════════════════════════════════════════════════════
# 5. Usage estimé
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_usage_prompt_tokens_positive() -> None:
    provider = MockEmbeddingsProvider()
    response = await provider.embed(["Un texte raisonnable qui va consommer quelques tokens."])
    assert response.usage.prompt_tokens > 0
    assert response.usage.total_tokens == response.usage.prompt_tokens


@pytest.mark.asyncio
async def test_mock_usage_scales_with_batch_size() -> None:
    provider = MockEmbeddingsProvider()
    one = await provider.embed(["abc"])
    five = await provider.embed(["abc"] * 5)
    assert five.usage.prompt_tokens >= one.usage.prompt_tokens * 5 - 1


# ══════════════════════════════════════════════════════════════
# 6. Rejet — liste vide
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_rejects_empty_texts_list() -> None:
    provider = MockEmbeddingsProvider()
    with pytest.raises(EmbeddingsInvalidRequestError):
        await provider.embed([])
