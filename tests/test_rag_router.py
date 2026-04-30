"""
Tests d'intégration — router `/rag/query` (Session D5).

TestClient FastAPI + guards surchargés + `RagQueryService.query`
monkeypatché. On vérifie :
- 200 + enveloppe avec chunks/sources/framed_context/instruction.
- 422 query vide / k hors bornes.
- 429 rate limit exceeded.
- 503 embeddings provider down.
- Forward file_ids au service.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    EmbeddingsUnavailableException,
    RateLimitAbuseException,
)
from app.features.auth.models import User
from app.features.rag.schemas import (
    RagChunkItem,
    RagQueryResponse,
    RagSourceItem,
)
from app.features.rag.service import RagQueryService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _fake_response(
    *,
    n_chunks: int = 2,
    with_framing: bool = True,
) -> RagQueryResponse:
    chunks: list[RagChunkItem] = []
    sources: list[RagSourceItem] = []
    for i in range(n_chunks):
        file_id = uuid.uuid4()
        chunks.append(
            RagChunkItem(
                id=i + 1,
                file_id=file_id,
                chunk_index=i,
                content=f"chunk {i}",
                token_count=50,
                start_char_offset=i * 100,
                end_char_offset=(i + 1) * 100,
                page_number=i + 1,
                similarity=0.9 - i * 0.1,
                original_filename=f"doc_{i}.pdf",
                mime_type="application/pdf",
            )
        )
        sources.append(
            RagSourceItem(
                file_id=file_id,
                chunk_index=i,
                start_char_offset=i * 100,
                end_char_offset=(i + 1) * 100,
                page_number=i + 1,
                similarity=0.9 - i * 0.1,
                original_filename=f"doc_{i}.pdf",
            )
        )
    framed = '<<<DOCUMENT EXTRACT id="1" ...>>>\nchunk 0\n<<<END EXTRACT 1>>>'
    return RagQueryResponse(
        chunks=chunks,
        sources=sources,
        framed_context=framed if with_framing else "",
        instruction="instruction système" if with_framing else "",
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    # Bypass rate limit par défaut — tests dédiés l'override.
    from app.features.rag import router as rag_router_module

    monkeypatch.setattr(
        rag_router_module,
        "check_user_rate_limit",
        AsyncMock(return_value=None),
    )

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# 1. Happy path
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_200_returns_chunks_sources_framed_and_instruction(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RagQueryService,
        "query",
        AsyncMock(return_value=_fake_response(n_chunks=3)),
    )
    response = client.post("/rag/query", json={"query": "un sujet"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["chunks"]) == 3
    assert len(body["data"]["sources"]) == 3
    assert body["data"]["chunks"][0]["content"] == "chunk 0"
    assert "<<<DOCUMENT EXTRACT" in body["data"]["framed_context"]
    assert body["data"]["instruction"] != ""


# ══════════════════════════════════════════════════════════════
# 2. Validations Pydantic
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_422_on_empty_query(client: TestClient) -> None:
    response = client.post("/rag/query", json={"query": ""})
    assert response.status_code == 422


def test_post_rag_query_422_on_k_over_20(client: TestClient) -> None:
    response = client.post("/rag/query", json={"query": "q", "k": 100})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 3. Rate limit exceeded
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_429_on_rate_limit_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.features.rag import router as rag_router_module

    monkeypatch.setattr(
        rag_router_module,
        "check_user_rate_limit",
        AsyncMock(side_effect=RateLimitAbuseException(retry_after=3600)),
    )
    response = client.post("/rag/query", json={"query": "q"})
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "RATE_LIMIT_ABUSE"


# ══════════════════════════════════════════════════════════════
# 4. Embeddings provider down → 503
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_propagates_embeddings_unavailable_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RagQueryService,
        "query",
        AsyncMock(side_effect=EmbeddingsUnavailableException(provider="mock", reason="test")),
    )
    response = client.post("/rag/query", json={"query": "q"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "EMBEDDINGS_UNAVAILABLE"


# ══════════════════════════════════════════════════════════════
# 5. Forward file_ids + k + min_similarity au service
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_forwards_file_ids_and_params_to_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_query = AsyncMock(return_value=_fake_response(n_chunks=0))
    monkeypatch.setattr(RagQueryService, "query", mock_query)
    fid1 = uuid.uuid4()
    fid2 = uuid.uuid4()
    response = client.post(
        "/rag/query",
        json={
            "query": "test",
            "k": 10,
            "min_similarity": 0.5,
            "file_ids": [str(fid1), str(fid2)],
        },
    )
    assert response.status_code == 200
    kwargs = mock_query.await_args.kwargs
    assert kwargs["k"] == 10
    assert kwargs["min_similarity"] == 0.5
    assert kwargs["file_ids"] == [fid1, fid2]


# ══════════════════════════════════════════════════════════════
# 6. Empty chunks → framed_context et instruction vides
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_returns_empty_framing_when_no_chunks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        RagQueryService,
        "query",
        AsyncMock(return_value=_fake_response(n_chunks=0, with_framing=False)),
    )
    response = client.post("/rag/query", json={"query": "q"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["chunks"] == []
    assert body["data"]["framed_context"] == ""
    assert body["data"]["instruction"] == ""


# ══════════════════════════════════════════════════════════════
# 7. Rate limit appelé avec la config settings
# ══════════════════════════════════════════════════════════════


def test_post_rag_query_calls_rate_limit_with_configured_per_hour(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.features.rag import router as rag_router_module

    mock_limit = AsyncMock(return_value=None)
    monkeypatch.setattr(rag_router_module, "check_user_rate_limit", mock_limit)
    monkeypatch.setattr(
        RagQueryService,
        "query",
        AsyncMock(return_value=_fake_response(n_chunks=0)),
    )
    response = client.post("/rag/query", json={"query": "q"})
    assert response.status_code == 200
    kwargs = mock_limit.await_args.kwargs
    assert kwargs["action"] == "rag_query"
    assert kwargs["max_requests"] == settings.rag_query_rate_limit_per_hour
    assert kwargs["window_seconds"] == 3600
