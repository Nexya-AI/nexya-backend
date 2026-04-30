"""
Tests unitaires — `ExpertCorpusService.search` (Session G1).

Vérifie la shape SQL produite par le service via `literal_binds` sans
Postgres réel : présence de `<=>`, `ORDER BY`, `LIMIT`, clause optionnelle
`language_pair`, cast `vector`, clamping `k`, isolation `expert_slug`.

Les tests d'intégration avec vrai pgvector + HNSW arriveront en lot G1.5
quand la CI DB sera stabilisée.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.experts.service import ExpertCorpusService


def _make_db_with_rows(rows: list[dict] | None = None) -> MagicMock:
    mappings_mock = MagicMock()
    mappings_mock.all.return_value = rows or []
    result_mock = MagicMock()
    result_mock.mappings.return_value = mappings_mock
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.mark.asyncio
async def test_search_empty_slug_returns_empty_list() -> None:
    db = _make_db_with_rows([])
    results = await ExpertCorpusService.search(db, expert_slug="", query_embedding=[0.0] * 768)
    assert results == []
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_search_returns_chunks_ordered_by_similarity() -> None:
    db = _make_db_with_rows(
        [
            {
                "id": 1,
                "content": "[FR] bonjour\n[ES] hola",
                "source": "tatoeba",
                "language_pair": "fra-spa",
                "metadata_json": {"src_id": 42},
                "similarity": 0.92,
            },
            {
                "id": 2,
                "content": "[FR] merci\n[ES] gracias",
                "source": "tatoeba",
                "language_pair": "fra-spa",
                "metadata_json": {"src_id": 43},
                "similarity": 0.81,
            },
        ]
    )
    results = await ExpertCorpusService.search(
        db,
        expert_slug="language",
        query_embedding=[0.1] * 768,
        k=5,
        min_similarity=0.7,
    )
    assert len(results) == 2
    assert results[0].similarity > results[1].similarity
    assert results[0].language_pair == "fra-spa"
    assert results[0].metadata == {"src_id": 42}


@pytest.mark.asyncio
async def test_search_sql_shape_contains_cosine_and_vector_cast() -> None:
    db = _make_db_with_rows([])
    await ExpertCorpusService.search(
        db,
        expert_slug="language",
        query_embedding=[0.1, 0.2, 0.3],
        k=3,
    )
    # Récupère la requête text() passée à db.execute
    call_args = db.execute.await_args_list[0]
    sql_obj = call_args.args[0]
    sql_str = str(sql_obj)
    assert "<=>" in sql_str
    assert "ORDER BY" in sql_str
    assert "LIMIT" in sql_str
    assert "CAST(:q_vec AS vector)" in sql_str
    assert "expert_slug = :slug" in sql_str


@pytest.mark.asyncio
async def test_search_language_pair_clause_conditional() -> None:
    db1 = _make_db_with_rows([])
    await ExpertCorpusService.search(db1, expert_slug="language", query_embedding=[0.0] * 4)
    sql_no_lang = str(db1.execute.await_args_list[0].args[0])
    assert "language_pair = :lang" not in sql_no_lang

    db2 = _make_db_with_rows([])
    await ExpertCorpusService.search(
        db2,
        expert_slug="language",
        query_embedding=[0.0] * 4,
        language_pair="fra-spa",
    )
    sql_with_lang = str(db2.execute.await_args_list[0].args[0])
    assert "language_pair = :lang" in sql_with_lang


@pytest.mark.asyncio
async def test_search_k_clamped_to_upper_bound() -> None:
    db = _make_db_with_rows([])
    await ExpertCorpusService.search(db, expert_slug="language", query_embedding=[0.0] * 4, k=999)
    call_args = db.execute.await_args_list[0]
    sql_obj = call_args.args[0]
    compiled = sql_obj.compile()
    params = compiled.params
    assert params.get("k") == 20  # clampé à _MAX_K


@pytest.mark.asyncio
async def test_search_k_clamped_to_lower_bound() -> None:
    db = _make_db_with_rows([])
    await ExpertCorpusService.search(db, expert_slug="language", query_embedding=[0.0] * 4, k=0)
    sql_obj = db.execute.await_args_list[0].args[0]
    assert sql_obj.compile().params.get("k") == 1


@pytest.mark.asyncio
async def test_search_min_similarity_passed_as_bindparam() -> None:
    db = _make_db_with_rows([])
    await ExpertCorpusService.search(
        db,
        expert_slug="language",
        query_embedding=[0.0] * 4,
        min_similarity=0.85,
    )
    sql_obj = db.execute.await_args_list[0].args[0]
    assert sql_obj.compile().params.get("min_sim") == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_search_metadata_non_dict_defensive_fallback() -> None:
    """Drivers qui renvoient metadata_json en str → on ne casse pas, dict vide."""
    db = _make_db_with_rows(
        [
            {
                "id": 1,
                "content": "x",
                "source": "tatoeba",
                "language_pair": None,
                "metadata_json": "corrupted-not-dict",
                "similarity": 0.8,
            }
        ]
    )
    results = await ExpertCorpusService.search(
        db, expert_slug="language", query_embedding=[0.0] * 4
    )
    assert results[0].metadata == {}
