"""
Tests d'intégration — router `/library` (Session C3).

Pattern identique aux tests projets : TestClient FastAPI + guards
surchargés, `LibraryService.*` monkeypatché en `AsyncMock`. On vérifie
que le routeur câble proprement, traduit ORM → Pydantic correctement,
et enrichit la réponse avec une presigned URL.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    FileTooLargeException,
    LibraryQuotaExceededException,
    ResourceNotFoundException,
)
from app.features.auth.models import User
from app.features.library import service as library_service_module
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_URL = "mock://nexya-media-test/users/abc/library/image/ab/fakeurl.png?expires=9999"


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_item(*, item_id: uuid.UUID | None = None) -> LibraryItem:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    item = LibraryItem(
        user_id=_FAKE_USER_ID,
        type="image",
        title="Chat roux",
        storage_key="c4a2.../library/image/ab/abc.png",
        mime_type="image/png",
        size_bytes=12345,
        content_sha256="a" * 64,
        source="generated",
    )
    item.id = item_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    item.created_at = now
    item.updated_at = now
    item.deleted_at = None
    item.file_type = None
    item.description = None
    item.width_px = 1024
    item.height_px = 1024
    item.duration_ms = None
    item.aspect_ratio = None
    item.provider = "gemini-imagen"
    item.model = "imagen-3.0-generate-002"
    item.prompt = "Un chaton roux dans un jardin"
    item.source_conversation_id = None
    item.source_message_id = None
    item.tags = ["chaton", "automne"]
    item.metadata_json = None
    return item


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _fake_user_override() -> User:
        return fake_user

    async def _fake_db_override():
        yield fake_db

    # presigned URL stubbée pour tous les tests — c'est le helper du
    # service qui aurait appelé l'ObjectStore réel.
    monkeypatch.setattr(
        LibraryService,
        "presigned_url_for",
        AsyncMock(return_value=_FAKE_URL),
    )

    app.dependency_overrides[get_current_user] = _fake_user_override
    app.dependency_overrides[get_db] = _fake_db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# 1. POST /library — create
# ══════════════════════════════════════════════════════════════


def test_create_library_returns_201_with_envelope_and_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _make_fake_item()
    monkeypatch.setattr(LibraryService, "create_from_base64", AsyncMock(return_value=item))

    response = client.post(
        "/library",
        json={
            "type": "image",
            "title": "Chat roux",
            "content_base64": base64.b64encode(b"binary").decode(),
            "mime_type": "image/png",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(item.id)
    assert body["data"]["type"] == "image"
    assert body["data"]["url"] == _FAKE_URL
    assert body["data"]["tags"] == ["chaton", "automne"]


def test_create_library_returns_402_on_quota(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LibraryService,
        "create_from_base64",
        AsyncMock(side_effect=LibraryQuotaExceededException(current=50, maximum=50, plan="free")),
    )

    response = client.post(
        "/library",
        json={
            "type": "image",
            "title": "Trop",
            "content_base64": base64.b64encode(b"x").decode(),
            "mime_type": "image/png",
        },
    )

    assert response.status_code == 402
    body = response.json()
    assert body["code"] == "LIBRARY_QUOTA_EXCEEDED"
    assert body["data"]["max"] == 50
    assert body["data"]["plan"] == "free"


def test_create_library_returns_413_on_too_large(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LibraryService,
        "create_from_base64",
        AsyncMock(side_effect=FileTooLargeException(max_mb=20)),
    )

    response = client.post(
        "/library",
        json={
            "type": "image",
            "title": "Trop gros",
            "content_base64": base64.b64encode(b"x").decode(),
            "mime_type": "image/png",
        },
    )

    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"


def test_create_library_rejects_mime_type_mismatch(
    client: TestClient,
) -> None:
    """Validator Pydantic : type=image + mime=application/pdf → 422."""
    response = client.post(
        "/library",
        json={
            "type": "image",
            "title": "Mismatch",
            "content_base64": base64.b64encode(b"%PDF").decode(),
            "mime_type": "application/pdf",
        },
    )
    assert response.status_code == 422


def test_create_library_rejects_document_without_file_type(
    client: TestClient,
) -> None:
    response = client.post(
        "/library",
        json={
            "type": "document",
            "title": "PDF sans file_type",
            "content_base64": base64.b64encode(b"%PDF").decode(),
            "mime_type": "application/pdf",
        },
    )
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 2. GET /library — list
# ══════════════════════════════════════════════════════════════


def test_list_library_forwards_filters(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = library_service_module.LibraryPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(LibraryService, "list_for_user", mock)

    conv_id = uuid.uuid4()
    response = client.get(
        f"/library?type=video&source=generated&conversation_id={conv_id}&q=noel&limit=10"
    )

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["type_"] == "video"
    assert kwargs["source"] == "generated"
    assert kwargs["conversation_id"] == conv_id
    assert kwargs["q"] == "noel"
    assert kwargs["limit"] == 10


def test_list_library_returns_page_with_items_and_urls(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _make_fake_item()
    page = library_service_module.LibraryPageOrm(items=[item], next_cursor="next-cur-abc")
    monkeypatch.setattr(LibraryService, "list_for_user", AsyncMock(return_value=page))

    response = client.get("/library")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["next_cursor"] == "next-cur-abc"
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["id"] == str(item.id)
    assert body["data"]["items"][0]["url"] == _FAKE_URL


def test_list_library_rejects_limit_above_50(client: TestClient) -> None:
    response = client.get("/library?limit=500")
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 3. GET /library/{id}
# ══════════════════════════════════════════════════════════════


def test_get_library_item_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _make_fake_item()
    monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))

    response = client.get(f"/library/{item.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["id"] == str(item.id)
    assert body["data"]["url"] == _FAKE_URL
    # Les champs sensibles NE DOIVENT PAS fuir.
    assert "storage_key" not in body["data"]
    assert "content_sha256" not in body["data"]


def test_get_library_item_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LibraryService,
        "get",
        AsyncMock(side_effect=ResourceNotFoundException("Média")),
    )

    response = client.get(f"/library/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 4. DELETE /library/{id}
# ══════════════════════════════════════════════════════════════


def test_delete_library_item_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LibraryService, "soft_delete", AsyncMock(return_value=None))

    response = client.delete(f"/library/{uuid.uuid4()}")
    assert response.status_code == 204
    assert response.content == b""


def test_delete_library_item_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        LibraryService,
        "soft_delete",
        AsyncMock(side_effect=ResourceNotFoundException("Média")),
    )

    response = client.delete(f"/library/{uuid.uuid4()}")
    assert response.status_code == 404
