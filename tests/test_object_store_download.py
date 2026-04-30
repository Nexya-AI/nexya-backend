"""
Tests unitaires — extension `ObjectStore.download_bytes` (D4).

Le worker `index_document_chunks` a besoin de récupérer le blob complet
pour re-extraire le texte avec marqueurs de page. On ajoute donc une
méthode `download_bytes` au contrat ObjectStore.
"""

from __future__ import annotations

import pytest

from app.core.storage.object_store import MockObjectStore


@pytest.mark.asyncio
async def test_mock_download_bytes_returns_uploaded_data() -> None:
    store = MockObjectStore()
    data = b"hello world"
    await store.upload_bytes("u1/file.pdf", data, mime_type="application/pdf")
    recovered = await store.download_bytes("u1/file.pdf")
    assert recovered == data


@pytest.mark.asyncio
async def test_mock_download_bytes_raises_file_not_found_on_missing_key() -> None:
    store = MockObjectStore()
    with pytest.raises(FileNotFoundError):
        await store.download_bytes("missing-key")


@pytest.mark.asyncio
async def test_mock_download_bytes_after_delete_raises() -> None:
    store = MockObjectStore()
    await store.upload_bytes("u1/file.pdf", b"data", mime_type="application/pdf")
    await store.delete_object("u1/file.pdf")
    with pytest.raises(FileNotFoundError):
        await store.download_bytes("u1/file.pdf")
