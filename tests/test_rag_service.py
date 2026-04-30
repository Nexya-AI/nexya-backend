"""
Tests unitaires — `RagQueryService.query` (D5).

Mock `get_embeddings_provider`, `get_budget_tracker`, et le DB execute
pour tester le service sans Postgres réel.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai.embeddings.base import (
    EmbeddingsResponse,
    EmbeddingsUnavailableError,
    EmbeddingsUsage,
    EmbeddingVector,
)
from app.core.errors.exceptions import (
    EmbeddingsUnavailableException,
    ValidationException,
)
from app.features.rag import service as rag_service_module
from app.features.rag.service import RagQueryService

_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user() -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = False
    return user


class _FakeProvider:
    name = "mock"
    default_model = "mock-test-embed"
    dim = 1536

    def __init__(self, *, raise_error: bool = False) -> None:
        self._raise = raise_error
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], *, model: str | None = None):
        self.calls.append(list(texts))
        if self._raise:
            raise EmbeddingsUnavailableError("provider down", provider=self.name)
        return EmbeddingsResponse(
            vectors=[
                EmbeddingVector(values=[0.0] * 1536, dim=1536, model=self.default_model)
                for _ in texts
            ],
            usage=EmbeddingsUsage(prompt_tokens=10, total_tokens=10),
        )


class _NoBudget:
    async def check_and_consume_embeddings(self, user_id: str, *, cost: int = 1) -> int:
        return 1


class _FakeDB:
    def __init__(self, *, rows: list[dict] | None = None) -> None:
        self._rows = rows or []
        self.executed_stmts: list[Any] = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed_stmts.append(stmt)
        result = MagicMock()
        mappings = MagicMock()
        mappings.all.return_value = self._rows
        result.mappings.return_value = mappings
        return result


@pytest.fixture(autouse=True)
def _bypass_budget(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rag_service_module, "get_budget_tracker", lambda: _NoBudget())


# ══════════════════════════════════════════════════════════════
# 1. Happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_happy_path_calls_embed_and_sql_then_builds_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)

    row = {
        "id": 42,
        "file_id": uuid.uuid4(),
        "chunk_index": 3,
        "content": "extrait de PDF",
        "token_count": 50,
        "start_char_offset": 100,
        "end_char_offset": 150,
        "page_number": 2,
        "original_filename": "rapport.pdf",
        "mime_type": "application/pdf",
        "similarity": 0.87,
    }
    db = _FakeDB(rows=[row])

    response = await RagQueryService.query(_make_user(), db, query="test query")

    assert len(response.chunks) == 1
    assert response.chunks[0].id == 42
    assert response.chunks[0].similarity == 0.87
    assert response.chunks[0].content == "extrait de PDF"
    assert len(response.sources) == 1
    assert response.sources[0].original_filename == "rapport.pdf"
    # Le framing a bien été appliqué.
    assert "<<<DOCUMENT EXTRACT" in response.framed_context
    assert response.instruction != ""
    # L'embed a été appelé 1 fois.
    assert len(provider.calls) == 1


# ══════════════════════════════════════════════════════════════
# 2. Empty query → 422
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_raises_validation_on_empty_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)
    db = _FakeDB()

    with pytest.raises(ValidationException):
        await RagQueryService.query(_make_user(), db, query="   ")


# ══════════════════════════════════════════════════════════════
# 3. Clamping k — max 20
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_clamps_k_at_max_20(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)
    db = _FakeDB(rows=[])
    # Le service doit clamper k=100 à 20 en interne.
    await RagQueryService.query(_make_user(), db, query="q", k=100)
    # Inspection du SQL compilé pour vérifier LIMIT 20.
    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert " limit 20" in compiled or "limit 20\n" in compiled


# ══════════════════════════════════════════════════════════════
# 4. Clamping k — min 1
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_clamps_k_at_min_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)
    db = _FakeDB(rows=[])
    await RagQueryService.query(_make_user(), db, query="q", k=-5)
    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "limit 1" in compiled


# ══════════════════════════════════════════════════════════════
# 5. Embed provider fail → 503
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_raises_503_when_embeddings_provider_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(raise_error=True)
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)
    db = _FakeDB()

    with pytest.raises(EmbeddingsUnavailableException) as ctx:
        await RagQueryService.query(_make_user(), db, query="q")
    assert ctx.value.status_code == 503


# ══════════════════════════════════════════════════════════════
# 6. SQL joins uploaded_files + filters deleted_at
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_sql_joins_uploaded_files_and_filters_deleted_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)
    db = _FakeDB(rows=[])
    await RagQueryService.query(_make_user(), db, query="q")

    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "join uploaded_files" in compiled
    assert "uf.deleted_at is null" in compiled
    assert "dc.user_id" in compiled


# ══════════════════════════════════════════════════════════════
# 7. Forward file_ids filter to SQL
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rag_query_forwards_file_ids_filter_to_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(rag_service_module, "get_embeddings_provider", lambda: provider)
    db = _FakeDB(rows=[])
    file_ids = [uuid.uuid4(), uuid.uuid4()]
    await RagQueryService.query(_make_user(), db, query="q", file_ids=file_ids)

    # SQLAlchemy ne supporte pas `literal_binds` sur une liste — on
    # inspecte donc le texte SQL brut + les bindparams.
    stmt = db.executed_stmts[0]
    sql_text = str(stmt).lower()
    assert "any(cast(" in sql_text
    assert "dc.file_id = any" in sql_text
    # Vérifier que file_ids est bien présent dans les bindparams.
    bindparams = stmt.compile().params
    assert "file_ids" in bindparams
    assert len(bindparams["file_ids"]) == 2
