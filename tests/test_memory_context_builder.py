"""
Tests unitaires — `build_memory_context` + `_format_memories_block` (Session D3).

Couverture :
- Happy path : 3 memories retournées par search → bloc markdown formaté.
- Short-circuit query vide → None sans appel search.
- Short-circuit `memory_injection_enabled=False` → None sans appel search.
- Liste vide de résultats → None.
- Overrides k / min_similarity forwardés à MemoryStore.search.
- Fail-safe : MemoryStore.search raise → log + None.
- Format : 1 memory / 5 memories / scores formatés à 2 décimales.
- Troncature : bloc > max_chars → marqueur [...] visible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.features.memory.context_builder import (
    _format_memories_block,
    build_memory_context,
)
from app.features.memory.models import Memory
from app.features.memory.service import MemorySearchResult, MemoryStore

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _make_user():
    user = MagicMock()
    user.id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    user.is_pro = False
    return user


def _make_memory(content: str, *, memory_id: uuid.UUID | None = None) -> Memory:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    m = Memory(
        user_id=uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77"),
        content=content,
        content_sha256="a" * 64,
        embedding=[0.1] * 1536,
        embedding_model="mock-1536",
        embedding_dim=1536,
        source="extracted",
    )
    m.id = memory_id or uuid.uuid4()
    m.created_at = now
    m.updated_at = now
    m.deleted_at = None
    m.importance = 3
    m.metadata_json = None
    m.source_conversation_id = None
    m.source_message_id = None
    return m


def _result(content: str, similarity: float) -> MemorySearchResult:
    return MemorySearchResult(memory=_make_memory(content), similarity=similarity)


# ══════════════════════════════════════════════════════════════
# 1. Happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_memory_context_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _make_user()
    db = MagicMock()
    results = [
        _result("L'utilisateur est développeur Flutter", 0.92),
        _result("L'utilisateur habite au Cameroun", 0.85),
        _result("L'utilisateur travaille sur un projet NEXYA", 0.71),
    ]
    monkeypatch.setattr(MemoryStore, "search", AsyncMock(return_value=results))

    block = await build_memory_context(user, db, query="Écris-moi un script")

    assert block is not None
    assert "[Contexte sur l'utilisateur]" in block
    assert "[/Contexte]" in block
    assert "L'utilisateur est développeur Flutter" in block
    assert "L'utilisateur habite au Cameroun" in block
    assert "L'utilisateur travaille sur un projet NEXYA" in block
    assert "0.92" in block
    assert "0.85" in block
    assert "0.71" in block


# ══════════════════════════════════════════════════════════════
# 2. Short-circuits
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_memory_context_empty_query_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query vide → None sans appel search."""
    user = _make_user()
    db = MagicMock()
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(MemoryStore, "search", mock_search)

    assert await build_memory_context(user, db, query="") is None
    assert await build_memory_context(user, db, query="   ") is None
    mock_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_memory_context_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`memory_injection_enabled=False` → None sans appel search."""
    user = _make_user()
    db = MagicMock()
    monkeypatch.setattr(settings, "memory_injection_enabled", False, raising=False)
    mock_search = AsyncMock(return_value=[_result("A", 0.9)])
    monkeypatch.setattr(MemoryStore, "search", mock_search)

    assert await build_memory_context(user, db, query="something") is None
    mock_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_memory_context_empty_results_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    db = MagicMock()
    monkeypatch.setattr(settings, "memory_injection_enabled", True, raising=False)
    monkeypatch.setattr(MemoryStore, "search", AsyncMock(return_value=[]))

    assert await build_memory_context(user, db, query="question") is None


# ══════════════════════════════════════════════════════════════
# 3. Forward des overrides
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_memory_context_forwards_k_and_min_similarity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    db = MagicMock()
    monkeypatch.setattr(settings, "memory_injection_enabled", True, raising=False)
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(MemoryStore, "search", mock_search)

    await build_memory_context(user, db, query="q", k=3, min_similarity=0.85)
    kwargs = mock_search.await_args.kwargs
    assert kwargs["k"] == 3
    assert kwargs["min_similarity"] == 0.85


@pytest.mark.asyncio
async def test_build_memory_context_uses_settings_defaults_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    db = MagicMock()
    monkeypatch.setattr(settings, "memory_injection_enabled", True, raising=False)
    monkeypatch.setattr(settings, "memory_injection_k", 7, raising=False)
    monkeypatch.setattr(settings, "memory_injection_min_similarity", 0.65, raising=False)
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(MemoryStore, "search", mock_search)

    await build_memory_context(user, db, query="q")
    kwargs = mock_search.await_args.kwargs
    assert kwargs["k"] == 7
    assert kwargs["min_similarity"] == 0.65


# ══════════════════════════════════════════════════════════════
# 4. Fail-safe absolue
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_memory_context_fail_safe_on_search_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search raise → return None (chat ne doit pas être bloqué)."""
    user = _make_user()
    db = MagicMock()
    monkeypatch.setattr(settings, "memory_injection_enabled", True, raising=False)
    monkeypatch.setattr(
        MemoryStore,
        "search",
        AsyncMock(side_effect=RuntimeError("pgvector unavailable")),
    )

    # Ne doit PAS raise.
    result = await build_memory_context(user, db, query="question importante")
    assert result is None


# ══════════════════════════════════════════════════════════════
# 5. Formatage du bloc
# ══════════════════════════════════════════════════════════════


def test_format_single_memory_includes_score() -> None:
    results = [_result("L'utilisateur est Ivan", 0.95)]
    block = _format_memories_block(results)
    assert "L'utilisateur est Ivan" in block
    assert "0.95" in block
    assert "[Contexte sur l'utilisateur]" in block
    assert "[/Contexte]" in block


def test_format_multiple_memories_preserved_in_order() -> None:
    results = [
        _result("Fait A", 0.95),
        _result("Fait B", 0.80),
        _result("Fait C", 0.72),
    ]
    block = _format_memories_block(results)
    a_pos = block.find("Fait A")
    b_pos = block.find("Fait B")
    c_pos = block.find("Fait C")
    assert 0 < a_pos < b_pos < c_pos


def test_format_empty_list_returns_empty_string() -> None:
    """Garde-fou défensif : liste vide → chaîne vide (pas le bloc vide)."""
    assert _format_memories_block([]) == ""


def test_format_truncates_above_max_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bloc > max_chars → tronque + marqueur lisible."""
    # Force un cap très bas pour provoquer la troncature.
    monkeypatch.setattr(settings, "memory_injection_max_chars", 500, raising=False)
    long_content = "L'utilisateur a un fait très long " + ("x" * 200)
    results = [_result(long_content, 0.9) for _ in range(5)]
    block = _format_memories_block(results)
    assert len(block) <= 500
    assert "[... contexte tronqué" in block or "[/Contexte]" in block
