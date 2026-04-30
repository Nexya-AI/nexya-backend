"""
Tests unitaires — `MockEmbeddingsProvider(dim=…)` configurable (G1).

Complète `test_embeddings_mock.py` (D1) qui valide le comportement 1536
par défaut. Ici on vérifie que la dim peut être choisie à l'instanciation
pour coller aux différentes colonnes `vector(N)` du projet.
"""

from __future__ import annotations

import math

import pytest

from app.ai.embeddings import MockEmbeddingsProvider


@pytest.mark.asyncio
async def test_default_dim_1536_backwards_compat() -> None:
    provider = MockEmbeddingsProvider()
    assert provider.dim == 1536
    response = await provider.embed(["hello"])
    assert response.vectors[0].dim == 1536
    assert len(response.vectors[0].values) == 1536


@pytest.mark.asyncio
async def test_custom_dim_768_for_gemini_corpus() -> None:
    provider = MockEmbeddingsProvider(dim=768)
    assert provider.dim == 768
    assert provider.default_model == "mock-768"
    response = await provider.embed(["hello"])
    assert response.vectors[0].dim == 768
    assert len(response.vectors[0].values) == 768


@pytest.mark.asyncio
async def test_custom_dim_l2_normalized() -> None:
    """Le Mock doit produire des vecteurs L2-normalisés quelle que soit la dim."""
    provider = MockEmbeddingsProvider(dim=768)
    response = await provider.embed(["sémantique"])
    v = response.vectors[0].values
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6


@pytest.mark.asyncio
async def test_task_type_silently_ignored() -> None:
    """Le Mock n'a pas de notion DOC vs QUERY — task_type est no-op."""
    provider = MockEmbeddingsProvider(dim=768)
    r1 = await provider.embed(["x"], task_type="RETRIEVAL_DOCUMENT")
    r2 = await provider.embed(["x"], task_type="RETRIEVAL_QUERY")
    assert r1.vectors[0].values == r2.vectors[0].values


def test_zero_or_negative_dim_raises() -> None:
    with pytest.raises(ValueError):
        MockEmbeddingsProvider(dim=0)
    with pytest.raises(ValueError):
        MockEmbeddingsProvider(dim=-10)
