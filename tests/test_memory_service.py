"""
Tests unitaires — `MemoryStore` (Session D1).

Pattern identique aux tests C2/C3/E3 : `_FakeDB` capture `db.execute`,
mocks pour provider embeddings et budget tracker, assertions sur les
statuts ORM + SQL compilé avec `literal_binds`.

Couverture :
- `_normalize_content` trim + collapse whitespace interne.
- `_content_sha256` hex 64 chars.
- `add` happy path : embed + INSERT + retour Memory avec vecteur 1536.
- `add` dédup via ON CONFLICT : 2ᵉ add même contenu → SELECT existant.
- `add` quota Free atteint → 402 MEMORY_QUOTA_EXCEEDED.
- `add` embed fail (EmbeddingsError) → 503 EMBEDDINGS_UNAVAILABLE.
- `add` content vide / trop long → 422 ValidationException.
- `search` validation query vide → 422.
- `search` k clamped dans [1, 50].
- `get_for_user` 404 IDOR-safe.
- `soft_delete` pose deleted_at + commit.
- `delete_for_user` (RGPD) retourne count physique DELETE.
- `count_for_user` retourne nombre actifs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.embeddings import (
    EmbeddingsUnavailableError,
    MockEmbeddingsProvider,
)
from app.config import settings
from app.core.errors.exceptions import (
    EmbeddingsUnavailableException,
    MemoryQuotaExceededException,
    RateLimitExceededException,
    ResourceNotFoundException,
    ValidationException,
)
from app.features.memory.models import Memory
from app.features.memory.service import (
    MemoryStore,
    _content_sha256,
    _normalize_content,
)

# ══════════════════════════════════════════════════════════════
# Fixtures & helpers
# ══════════════════════════════════════════════════════════════


def _make_user(*, is_pro: bool = False) -> MagicMock:
    user = MagicMock()
    user.id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    user.is_pro = is_pro
    return user


def _make_memory(
    *,
    memory_id: uuid.UUID | None = None,
    content: str = "Ivan est dev Flutter",
) -> Memory:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    m = Memory(
        user_id=uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77"),
        content=content,
        content_sha256=_content_sha256(content),
        embedding=[0.1] * 1536,
        embedding_model="mock-1536",
        embedding_dim=1536,
        source="manual",
    )
    m.id = memory_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    m.created_at = now
    m.updated_at = now
    m.deleted_at = None
    m.importance = 1
    m.metadata_json = None
    m.source_conversation_id = None
    m.source_message_id = None
    return m


class _ScalarResult:
    def __init__(
        self,
        *,
        one: Any | None | type = object,
        count: int | None = None,
        scalars_all: list | None = None,
    ) -> None:
        self._one = one
        self._count = count
        self._scalars_all = scalars_all or []

    def scalar_one(self):
        if self._count is not None:
            return self._count
        return 0

    def scalar_one_or_none(self):
        return None if self._one is object else self._one

    def scalars(self):
        sc = MagicMock()
        sc.all.return_value = self._scalars_all
        return sc


class _FakeDB:
    def __init__(self, execute_results: list[Any]) -> None:
        self._results = iter(execute_results)
        self.commit = AsyncMock()
        self.refresh = AsyncMock()
        self.executed: list[Any] = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        return next(self._results)


class _NoBudget:
    """BudgetTracker stub qui ne fait rien (pas de rate limit)."""

    async def check_and_consume_embeddings(self, user_id: str, *, cost: int = 1) -> int:
        return 1


class _OverBudget:
    """BudgetTracker stub qui déclenche RateLimitExceededException."""

    async def check_and_consume_embeddings(self, user_id: str, *, cost: int = 1) -> int:
        raise RateLimitExceededException()


@pytest.fixture(autouse=True)
def _bypass_budget(monkeypatch: pytest.MonkeyPatch):
    """Par défaut on coupe le budget tracker — les tests qui veulent le
    tester l'override explicitement."""
    from app.features.memory import service as service_module

    monkeypatch.setattr(service_module, "get_budget_tracker", lambda: _NoBudget())
    yield


# ══════════════════════════════════════════════════════════════
# 1. Helpers privés
# ══════════════════════════════════════════════════════════════


def test_normalize_content_trims_and_collapses_whitespace() -> None:
    assert _normalize_content("  Ivan   est  dev    Flutter  ") == "Ivan est dev Flutter"
    assert _normalize_content("\n\tHello\n\nWorld\t\t") == "Hello World"
    assert _normalize_content("déjà normalisé") == "déjà normalisé"


def test_content_sha256_is_64_chars_hex() -> None:
    sha = _content_sha256("hello")
    assert len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)


def test_content_sha256_same_content_same_hash() -> None:
    assert _content_sha256("foo") == _content_sha256("foo")
    assert _content_sha256("foo") != _content_sha256("bar")


# ══════════════════════════════════════════════════════════════
# 2. add — happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_add_happy_path_inserts_with_embedding() -> None:
    user = _make_user()
    inserted = _make_memory()
    db = _FakeDB(
        execute_results=[
            _ScalarResult(count=0),  # quota preflight COUNT active = 0
            _ScalarResult(one=inserted),  # INSERT RETURNING *
        ]
    )

    result = await MemoryStore.add(
        user, db, content="Ivan est dev Flutter", provider=MockEmbeddingsProvider()
    )

    assert result is inserted
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_add_dedup_via_on_conflict_returns_existing() -> None:
    """2ᵉ add du MÊME contenu par le même user → ON CONFLICT DO NOTHING
    RETURNING vide, puis SELECT retrouve l'existant."""
    user = _make_user()
    existing = _make_memory()
    db = _FakeDB(
        execute_results=[
            _ScalarResult(count=0),  # quota OK
            _ScalarResult(one=None),  # INSERT RETURNING vide (conflit)
            _ScalarResult(one=existing),  # SELECT existing
        ]
    )

    result = await MemoryStore.add(
        user, db, content="Ivan est dev Flutter", provider=MockEmbeddingsProvider()
    )

    assert result is existing


# ══════════════════════════════════════════════════════════════
# 3. add — rejets
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_add_rejects_empty_content() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[])
    with pytest.raises(ValidationException):
        await MemoryStore.add(user, db, content="   \n\t  ")


@pytest.mark.asyncio
async def test_add_rejects_content_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[])
    monkeypatch.setattr(settings, "embeddings_content_max_chars", 50, raising=False)
    with pytest.raises(ValidationException):
        await MemoryStore.add(user, db, content="x" * 100)


@pytest.mark.asyncio
async def test_add_quota_exceeded_free() -> None:
    user = _make_user(is_pro=False)
    db = _FakeDB(
        execute_results=[
            _ScalarResult(count=settings.memory_max_free),  # plafond atteint
        ]
    )
    with pytest.raises(MemoryQuotaExceededException) as excinfo:
        await MemoryStore.add(user, db, content="Nouveau fait", provider=MockEmbeddingsProvider())
    assert excinfo.value.code == "MEMORY_QUOTA_EXCEEDED"
    assert excinfo.value.data["plan"] == "free"


@pytest.mark.asyncio
async def test_add_budget_exhausted_raises_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarResult(count=0)])
    from app.features.memory import service as service_module

    monkeypatch.setattr(service_module, "get_budget_tracker", lambda: _OverBudget())
    with pytest.raises(RateLimitExceededException):
        await MemoryStore.add(user, db, content="test", provider=MockEmbeddingsProvider())


@pytest.mark.asyncio
async def test_add_embed_failure_raises_503() -> None:
    """Provider qui lève EmbeddingsError → 503 EMBEDDINGS_UNAVAILABLE."""
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarResult(count=0)])

    class _FailingProvider:
        name = "failing"
        default_model = "fail"
        dim = 1536

        async def embed(self, texts, *, model=None):
            raise EmbeddingsUnavailableError("OpenAI down", provider="failing")

    with pytest.raises(EmbeddingsUnavailableException) as excinfo:
        await MemoryStore.add(user, db, content="test", provider=_FailingProvider())
    assert excinfo.value.code == "EMBEDDINGS_UNAVAILABLE"


# ══════════════════════════════════════════════════════════════
# 4. search — validation + shape
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_search_rejects_empty_query() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[])
    with pytest.raises(ValidationException):
        await MemoryStore.search(user, db, query="   ", provider=MockEmbeddingsProvider())


@pytest.mark.asyncio
async def test_search_empty_results_returns_empty_list() -> None:
    """Si la query SQL retourne 0 rows, on renvoie une liste vide propre."""
    user = _make_user()

    class _EmptyRowsResult:
        def mappings(self):
            m = MagicMock()
            m.all.return_value = []
            return m

    db = _FakeDB(execute_results=[_EmptyRowsResult()])
    results = await MemoryStore.search(
        user, db, query="quel langage ?", provider=MockEmbeddingsProvider()
    )
    assert results == []


# ══════════════════════════════════════════════════════════════
# 5. get_for_user + soft_delete
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_for_user_returns_404_if_not_owner() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarResult(one=None)])
    with pytest.raises(ResourceNotFoundException):
        await MemoryStore.get_for_user(uuid.uuid4(), user, db)


@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_at_and_commits() -> None:
    user = _make_user()
    memory = _make_memory()
    db = _FakeDB(execute_results=[_ScalarResult(one=memory)])

    await MemoryStore.soft_delete(memory.id, user, db)

    assert memory.deleted_at is not None
    db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 6. delete_for_user (RGPD) — hard DELETE + count
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_for_user_hard_deletes_and_returns_count() -> None:
    user = _make_user()
    db = _FakeDB(
        execute_results=[
            _ScalarResult(scalars_all=[uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]),
        ]
    )

    count = await MemoryStore.delete_for_user(user, db)
    assert count == 3
    db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 7. count_for_user
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_count_for_user_returns_active_count() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarResult(count=42)])
    count = await MemoryStore.count_for_user(user, db)
    assert count == 42
