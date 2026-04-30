"""
Tests d'intégration — router `/memory/*` (Session D5).

TestClient FastAPI + guards surchargés + `MemoryStore` monkeypatché.
On vérifie :
- `POST /memory/index` : 201 + enveloppe + 422 empty + propagation 402/429.
- `POST /memory/search` : 200 + items avec similarity + forward k/source.
- `GET /memory` : 200 + pagination + filtre source.
- `DELETE /memory/{id}` : 204 + idempotence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    MemoryQuotaExceededException,
    RateLimitExceededException,
)
from app.features.auth.models import User
from app.features.memory.models import Memory
from app.features.memory.service import (
    MemoriesPage,
    MemorySearchResult,
    MemoryStore,
)
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_memory(
    *,
    content: str = "L'utilisateur est dev Flutter",
    source: str = "manual",
    importance: int = 1,
) -> Memory:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    m = Memory(
        user_id=_FAKE_USER_ID,
        content=content,
        content_sha256="a" * 64,
        embedding=[0.0] * 1536,
        embedding_model="mock",
        embedding_dim=1536,
        source=source,
        importance=importance,
    )
    m.id = uuid.uuid4()
    m.created_at = now
    m.updated_at = now
    m.deleted_at = None
    m.source_conversation_id = None
    m.source_message_id = None
    m.metadata_json = None
    return m


@pytest.fixture
def client() -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# POST /memory/index
# ══════════════════════════════════════════════════════════════


def test_post_memory_index_201_with_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = _make_fake_memory(content="L'utilisateur habite au Cameroun")
    monkeypatch.setattr(MemoryStore, "add", AsyncMock(return_value=memory))
    response = client.post(
        "/memory/index",
        json={"content": "L'utilisateur habite au Cameroun"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["content"] == "L'utilisateur habite au Cameroun"
    assert body["data"]["source"] == "manual"
    assert body["data"]["id"] == str(memory.id)


def test_post_memory_index_422_on_empty_content(client: TestClient) -> None:
    response = client.post("/memory/index", json={"content": ""})
    assert response.status_code == 422


def test_post_memory_index_422_on_content_over_2000_chars(
    client: TestClient,
) -> None:
    response = client.post("/memory/index", json={"content": "x" * 2001})
    assert response.status_code == 422


def test_post_memory_index_propagates_402_quota_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        MemoryStore,
        "add",
        AsyncMock(side_effect=MemoryQuotaExceededException(current=100, maximum=100, plan="free")),
    )
    response = client.post("/memory/index", json={"content": "ok"})
    assert response.status_code == 402
    body = response.json()
    assert body["code"] == "MEMORY_QUOTA_EXCEEDED"
    assert body["data"]["plan"] == "free"


def test_post_memory_index_propagates_429_budget_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        MemoryStore,
        "add",
        AsyncMock(side_effect=RateLimitExceededException(reset_at=None)),
    )
    response = client.post("/memory/index", json={"content": "ok"})
    assert response.status_code == 429


# ══════════════════════════════════════════════════════════════
# POST /memory/search
# ══════════════════════════════════════════════════════════════


def test_post_memory_search_200_returns_items_with_similarity(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    m1 = _make_fake_memory(content="fact un")
    m2 = _make_fake_memory(content="fact deux")
    monkeypatch.setattr(
        MemoryStore,
        "search",
        AsyncMock(
            return_value=[
                MemorySearchResult(memory=m1, similarity=0.95),
                MemorySearchResult(memory=m2, similarity=0.82),
            ]
        ),
    )
    response = client.post("/memory/search", json={"query": "où j'habite ?"})
    assert response.status_code == 200
    body = response.json()
    items = body["data"]["items"]
    assert len(items) == 2
    assert items[0]["similarity"] == 0.95
    assert items[0]["memory"]["content"] == "fact un"
    assert items[1]["similarity"] == 0.82


def test_post_memory_search_422_on_empty_query(client: TestClient) -> None:
    response = client.post("/memory/search", json={"query": ""})
    assert response.status_code == 422


def test_post_memory_search_forwards_k_and_source_to_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(MemoryStore, "search", mock_search)
    response = client.post(
        "/memory/search",
        json={
            "query": "test",
            "k": 10,
            "min_similarity": 0.5,
            "source": "manual",
        },
    )
    assert response.status_code == 200
    kwargs = mock_search.await_args.kwargs
    assert kwargs["k"] == 10
    assert kwargs["min_similarity"] == 0.5
    assert kwargs["source"] == "manual"


# ══════════════════════════════════════════════════════════════
# GET /memory
# ══════════════════════════════════════════════════════════════


def test_get_memory_200_returns_paginated_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    m1 = _make_fake_memory(content="a")
    m2 = _make_fake_memory(content="b")
    monkeypatch.setattr(
        MemoryStore,
        "list_for_user",
        AsyncMock(return_value=MemoriesPage(items=[m1, m2], next_cursor="opaque-next")),
    )
    response = client.get("/memory")
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]["items"]) == 2
    assert body["data"]["next_cursor"] == "opaque-next"


def test_get_memory_forwards_cursor_limit_source_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_list = AsyncMock(return_value=MemoriesPage(items=[], next_cursor=None))
    monkeypatch.setattr(MemoryStore, "list_for_user", mock_list)
    response = client.get("/memory?cursor=abc&limit=15&source=extracted")
    assert response.status_code == 200
    kwargs = mock_list.await_args.kwargs
    assert kwargs["cursor"] == "abc"
    assert kwargs["limit"] == 15
    assert kwargs["source"] == "extracted"


def test_get_memory_422_on_limit_over_50(client: TestClient) -> None:
    response = client.get("/memory?limit=500")
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# DELETE /memory/{id}
# ══════════════════════════════════════════════════════════════


def test_delete_memory_204_on_existing(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MemoryStore, "delete_one_for_user", AsyncMock(return_value=1))
    mid = uuid.uuid4()
    response = client.delete(f"/memory/{mid}")
    assert response.status_code == 204
    assert response.content == b""


def test_delete_memory_204_on_already_deleted_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mémoire inexistante ou déjà supprimée → 204 (idempotent, pas 404)."""
    monkeypatch.setattr(MemoryStore, "delete_one_for_user", AsyncMock(return_value=0))
    mid = uuid.uuid4()
    response = client.delete(f"/memory/{mid}")
    assert response.status_code == 204


def test_delete_memory_422_on_malformed_uuid(client: TestClient) -> None:
    response = client.delete("/memory/not-a-uuid")
    assert response.status_code == 422
