"""
Tests unitaires — `ObjectStore` (Session C3).

On teste exclusivement `MockObjectStore` + la factory `get_object_store()`
qui doit retourner le mock quand les creds S3 sont absentes. Les tests
S3 en live (contre un vrai MinIO / localstack) sont hors scope du pytest
unitaire — ils viendront en intégration via `docker compose up`.
"""

from __future__ import annotations

import pytest

from app.core.storage import (
    MockObjectStore,
    ObjectStore,
    get_object_store,
    reset_object_store,
)

# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_store() -> MockObjectStore:
    """Instance fraîche pour chaque test."""
    return MockObjectStore(bucket="test-bucket")


# ══════════════════════════════════════════════════════════════
# 1. Contrat de base
# ══════════════════════════════════════════════════════════════


def test_mock_store_implements_object_store_interface(
    mock_store: MockObjectStore,
) -> None:
    """Sanity check : MockObjectStore est bien un `ObjectStore` utilisable."""
    assert isinstance(mock_store, ObjectStore)
    assert mock_store.name == "mock"


@pytest.mark.asyncio
async def test_upload_then_exists_returns_true(
    mock_store: MockObjectStore,
) -> None:
    await mock_store.upload_bytes("users/abc/photo.png", b"fake-image-bytes", mime_type="image/png")
    assert await mock_store.object_exists("users/abc/photo.png") is True


@pytest.mark.asyncio
async def test_exists_returns_false_for_missing_key(
    mock_store: MockObjectStore,
) -> None:
    assert await mock_store.object_exists("does/not/exist") is False


@pytest.mark.asyncio
async def test_stat_object_returns_none_for_missing(
    mock_store: MockObjectStore,
) -> None:
    assert await mock_store.stat_object("void") is None


@pytest.mark.asyncio
async def test_stat_object_returns_full_stat_after_upload(
    mock_store: MockObjectStore,
) -> None:
    data = b"hello world"
    await mock_store.upload_bytes("plain.txt", data, mime_type="text/plain")
    stat = await mock_store.stat_object("plain.txt")
    assert stat is not None
    assert stat.key == "plain.txt"
    assert stat.size_bytes == len(data)
    assert stat.mime_type == "text/plain"
    assert stat.last_modified is not None


# ══════════════════════════════════════════════════════════════
# 2. Delete idempotent
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_object_is_idempotent(mock_store: MockObjectStore) -> None:
    await mock_store.upload_bytes("foo", b"bar", mime_type="application/octet-stream")
    assert await mock_store.object_exists("foo") is True
    await mock_store.delete_object("foo")
    assert await mock_store.object_exists("foo") is False
    # Second delete — no-op, pas d'erreur.
    await mock_store.delete_object("foo")


@pytest.mark.asyncio
async def test_delete_missing_key_no_op(mock_store: MockObjectStore) -> None:
    """Un delete sur clé absente ne lève jamais — contrat S3 idempotent."""
    await mock_store.delete_object("never-uploaded")  # pas de raise


# ══════════════════════════════════════════════════════════════
# 3. Presigned URL — format mock://... ?expires=...
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_presigned_url_contains_bucket_key_and_expiry(
    mock_store: MockObjectStore,
) -> None:
    url = await mock_store.generate_presigned_url("users/xyz/vid.mp4", ttl_seconds=900)
    assert url.startswith("mock://test-bucket/users/xyz/vid.mp4")
    assert "expires=" in url
    assert "method=GET" in url


@pytest.mark.asyncio
async def test_presigned_url_supports_put_method(
    mock_store: MockObjectStore,
) -> None:
    url = await mock_store.generate_presigned_url("upload-target", method="PUT")
    assert "method=PUT" in url


# ══════════════════════════════════════════════════════════════
# 4. Factory — mock auto quand s3_access_key vide
# ══════════════════════════════════════════════════════════════


def test_get_object_store_returns_mock_when_access_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "s3_access_key", "", raising=False)
    monkeypatch.setattr(settings, "storage_mock_enabled", False, raising=False)
    reset_object_store()
    store = get_object_store()
    assert isinstance(store, MockObjectStore)


def test_get_object_store_returns_mock_when_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    # Même avec une clé posée, `storage_mock_enabled=True` force le mock.
    monkeypatch.setattr(settings, "s3_access_key", "some-key", raising=False)
    monkeypatch.setattr(settings, "storage_mock_enabled", True, raising=False)
    reset_object_store()
    store = get_object_store()
    assert isinstance(store, MockObjectStore)


def test_get_object_store_is_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "s3_access_key", "", raising=False)
    monkeypatch.setattr(settings, "storage_mock_enabled", False, raising=False)
    reset_object_store()
    a = get_object_store()
    b = get_object_store()
    assert a is b
