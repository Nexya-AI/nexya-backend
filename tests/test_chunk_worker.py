"""
Tests unitaires — worker `index_document_chunks` (D4).

Monkey-patche `AsyncSessionLocal`, l'ObjectStore et le provider
embeddings pour isoler le worker de toute I/O réelle.

Couvre :
- Short-circuits : missing / deleted / already_indexed / mime_not_supported.
- Semaphore Redis : acquisition OK, saturation → arq.Retry(30).
- Happy path : download → extract → clean → chunk → embed → INSERT +
  sentinelle posée.
- Cap truncation : > documents_chunks_per_file_max → truncate + log.
- Cancellation mid-chunking : soft-delete pendant embed → skip.
- Fail-safe storage : FileNotFoundError → skip + sentinelle.
- Texte trop court : skip + sentinelle posée (évite re-tentative infinie).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai.embeddings.base import (
    EmbeddingsResponse,
    EmbeddingsUsage,
    EmbeddingVector,
)
from app.features.files.chunker import Chunk
from app.features.files.models import UploadedFile
from workers import chunk_tasks

# ══════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════


_FILE_ID = uuid.UUID("11111111-0000-4000-8000-000000000001")
_USER_ID = uuid.UUID("22222222-0000-4000-8000-000000000002")


def _make_uploaded_file(
    *,
    mime: str = "application/pdf",
    deleted: bool = False,
    indexed: bool = False,
) -> UploadedFile:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    row = UploadedFile(
        user_id=_USER_ID,
        storage_key=f"{_USER_ID}/uploads/ab/abc123.pdf",
        content_sha256="a" * 64,
        size_bytes=1024,
        mime_type=mime,
        original_filename="test.pdf",
        extension="pdf",
        virus_scan_status="clean",
        extraction_status="ok",
    )
    row.id = _FILE_ID
    row.created_at = now
    row.updated_at = now
    row.deleted_at = now if deleted else None
    row.chunks_indexed_at = now if indexed else None
    row.extracted_text = "Lorem ipsum " * 50
    row.extracted_text_length = len(row.extracted_text)
    row.page_count = 3
    row.extraction_truncated = False
    row.extracted_at = now
    row.attached_to_kind = None
    row.attached_to_id = None
    row.attached_at = None
    return row


class _FakeDB:
    """AsyncSession fake — route les `get` et les `execute` selon besoin."""

    def __init__(self, *, file_row: UploadedFile | None) -> None:
        self._file_row = file_row
        self.executed = []
        self.added: list[Any] = []
        self.commits = 0

    async def __aenter__(self) -> _FakeDB:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def get(self, model, key):
        if model is UploadedFile:
            return self._file_row
        return None

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        sql = str(stmt).lower()
        result = MagicMock()
        # SELECT deleted_at pour _is_file_cancelled.
        if "select uploaded_files.deleted_at" in sql:
            result.scalar_one_or_none.return_value = (
                self._file_row.deleted_at if self._file_row else None
            )
        else:
            result.scalar_one_or_none.return_value = None
            result.scalar_one.return_value = 0
        return result

    def add(self, row) -> None:
        self.added.append(row)

    def add_all(self, rows) -> None:
        self.added.extend(rows)

    async def commit(self) -> None:
        self.commits += 1


class _FakeObjectStore:
    def __init__(self, *, data: bytes = b"", raise_not_found: bool = False) -> None:
        self._data = data
        self._raise_not_found = raise_not_found

    async def download_bytes(self, key: str) -> bytes:
        if self._raise_not_found:
            raise FileNotFoundError(key)
        return self._data


class _FakeEmbeddingsProvider:
    def __init__(
        self,
        *,
        default_model: str = "mock-test-embed",
        call_log: list[int] | None = None,
    ) -> None:
        self.default_model = default_model
        self.name = "mock"
        self.dim = 1536
        self.call_log = call_log if call_log is not None else []

    async def embed(self, texts: list[str], *, model: str | None = None):
        self.call_log.append(len(texts))
        vectors = [
            EmbeddingVector(
                values=[0.0] * 1536,
                dim=1536,
                model=self.default_model,
            )
            for _ in texts
        ]
        usage = EmbeddingsUsage(
            prompt_tokens=10 * len(texts),
            total_tokens=10 * len(texts),
        )
        return EmbeddingsResponse(vectors=vectors, usage=usage)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    async def incrby(self, key: str, n: int) -> int:
        new = self.store.get(key, 0) + n
        self.store[key] = new
        return new

    async def decrby(self, key: str, n: int) -> int:
        new = self.store.get(key, 0) - n
        self.store[key] = new
        return new

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def set(self, key: str, value: int) -> bool:
        self.store[key] = value
        return True


@pytest.fixture
def patch_worker(monkeypatch: pytest.MonkeyPatch):
    """Context manager qui installe tous les mocks worker."""

    def _install(
        *,
        file_row: UploadedFile | None,
        object_store: _FakeObjectStore | None = None,
        redis: _FakeRedis | None = None,
        embeddings: _FakeEmbeddingsProvider | None = None,
    ) -> dict[str, Any]:
        db = _FakeDB(file_row=file_row)

        def _factory():
            return db

        monkeypatch.setattr(chunk_tasks, "AsyncSessionLocal", _factory)

        store = object_store or _FakeObjectStore(
            data=(b"%PDF-1.4 placeholder for tests, real content via extract_text mock.")
        )
        monkeypatch.setattr(chunk_tasks, "get_object_store", lambda: store)

        r = redis or _FakeRedis()
        monkeypatch.setattr(chunk_tasks, "get_redis", lambda: r)

        provider = embeddings or _FakeEmbeddingsProvider()
        monkeypatch.setattr(chunk_tasks, "get_embeddings_provider", lambda: provider)

        return {
            "db": db,
            "store": store,
            "redis": r,
            "provider": provider,
        }

    return _install


# ══════════════════════════════════════════════════════════════
# 1. Short-circuits
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_skip_if_file_missing(patch_worker) -> None:
    patch_worker(file_row=None)
    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result == {"skipped": True, "reason": "missing"}


@pytest.mark.asyncio
async def test_worker_skip_if_file_deleted(patch_worker) -> None:
    row = _make_uploaded_file(deleted=True)
    patch_worker(file_row=row)
    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result == {"skipped": True, "reason": "deleted"}


@pytest.mark.asyncio
async def test_worker_skip_if_already_indexed(patch_worker) -> None:
    row = _make_uploaded_file(indexed=True)
    patch_worker(file_row=row)
    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result == {"skipped": True, "reason": "already_indexed"}


@pytest.mark.asyncio
async def test_worker_skip_if_mime_not_supported(patch_worker) -> None:
    row = _make_uploaded_file(mime="image/png")
    patch_worker(file_row=row)
    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result == {"skipped": True, "reason": "mime_not_supported"}


# ══════════════════════════════════════════════════════════════
# 2. Sémaphore Redis
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_retries_when_semaphore_saturated(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arq import Retry

    from app.config import settings

    # Simule un compteur déjà au max pour cet user.
    redis = _FakeRedis()
    redis.store[f"{chunk_tasks._SEMAPHORE_KEY_PREFIX}{_USER_ID}"] = (
        settings.max_concurrent_chunking_per_user
    )

    row = _make_uploaded_file()
    patch_worker(file_row=row, redis=redis)

    with pytest.raises(Retry):
        await chunk_tasks.index_document_chunks({}, str(_FILE_ID))


@pytest.mark.asyncio
async def test_worker_releases_semaphore_on_exception(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Même si le pipeline crash, le compteur sémaphore doit décrémenter."""
    row = _make_uploaded_file()
    mocks = patch_worker(file_row=row)
    # Fait crasher l'extraction.
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    # Le compteur doit être 0 (incrémenté puis décrémenté).
    key = f"{chunk_tasks._SEMAPHORE_KEY_PREFIX}{_USER_ID}"
    assert mocks["redis"].store.get(key, 0) == 0


# ══════════════════════════════════════════════════════════════
# 3. Happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_happy_path_inserts_chunks_and_sets_sentinel(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _make_uploaded_file(mime="text/plain")
    mocks = patch_worker(file_row=row)

    # Mock de l'extraction — retourne un texte long qui produira plusieurs
    # chunks.
    from app.core.storage.text_extractor import ExtractedText

    long_text = "Lorem ipsum dolor sit amet. " * 200  # ~5600 chars
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text=long_text, page_count=None, truncated=False, status="ok"
        ),
    )

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result["skipped"] is False
    assert result["n_chunks"] >= 2
    assert result["total_tokens"] > 0
    assert result["embeddings_cost_usd"] >= 0
    # Des rows DocumentChunk ajoutées.
    assert len(mocks["db"].added) == result["n_chunks"]
    # commit appelé au moins une fois (insert + sentinelle).
    assert mocks["db"].commits >= 1


# ══════════════════════════════════════════════════════════════
# 4. Batch embedding ≤ 100
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_calls_embeddings_in_batches_of_configured_size(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.core.storage.text_extractor import ExtractedText

    # Configurer un petit batch size pour tester plusieurs batches.
    monkeypatch.setattr(settings, "documents_embed_batch_size", 5)

    row = _make_uploaded_file(mime="text/plain")

    call_log: list[int] = []
    provider = _FakeEmbeddingsProvider(call_log=call_log)
    mocks = patch_worker(file_row=row, embeddings=provider)

    # Forcer un texte qui produit ≥ 12 chunks.
    def _fake_chunker(*args, **kwargs):
        return [
            Chunk(
                index=i,
                content=f"chunk {i}",
                token_count=10,
                start_char_offset=i * 10,
                end_char_offset=(i + 1) * 10,
                page_number=None,
            )
            for i in range(12)
        ]

    monkeypatch.setattr(chunk_tasks, "chunk_text", _fake_chunker)
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text="x" * 1000,
            page_count=None,
            truncated=False,
            status="ok",
        ),
    )

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result["skipped"] is False
    # 12 chunks, batch size 5 → 3 appels (5, 5, 2).
    assert call_log == [5, 5, 2]


# ══════════════════════════════════════════════════════════════
# 5. Cap truncation
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_truncates_at_chunks_per_file_max(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings
    from app.core.storage.text_extractor import ExtractedText

    monkeypatch.setattr(settings, "documents_chunks_per_file_max", 3)

    row = _make_uploaded_file(mime="text/plain")
    mocks = patch_worker(file_row=row)

    # Forcer 10 chunks > cap=3.
    def _fake_chunker(*args, **kwargs):
        return [
            Chunk(
                index=i,
                content=f"chunk {i}",
                token_count=10,
                start_char_offset=i * 10,
                end_char_offset=(i + 1) * 10,
                page_number=None,
            )
            for i in range(10)
        ]

    monkeypatch.setattr(chunk_tasks, "chunk_text", _fake_chunker)
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text="x" * 1000,
            page_count=None,
            truncated=False,
            status="ok",
        ),
    )

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result["skipped"] is False
    assert result["n_chunks"] == 3
    assert result["truncated"] is True
    assert len(mocks["db"].added) == 3


# ══════════════════════════════════════════════════════════════
# 6. Texte trop court
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_skips_when_cleaned_text_below_min_chars(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Texte < documents_pre_clean_min_chars → skip + sentinelle posée."""
    from app.config import settings
    from app.core.storage.text_extractor import ExtractedText

    monkeypatch.setattr(settings, "documents_pre_clean_min_chars", 100)

    row = _make_uploaded_file(mime="text/plain")
    mocks = patch_worker(file_row=row)
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text="trop court", page_count=None, truncated=False, status="ok"
        ),
    )

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result["skipped"] is True
    assert result["reason"] == "text_too_short"
    # Sentinelle posée (1 UPDATE dans _mark_indexed).
    assert mocks["db"].commits >= 1


# ══════════════════════════════════════════════════════════════
# 7. Storage missing
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_skips_when_storage_missing(patch_worker) -> None:
    row = _make_uploaded_file()
    store = _FakeObjectStore(raise_not_found=True)
    patch_worker(file_row=row, object_store=store)

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result == {"skipped": True, "reason": "storage_missing"}


# ══════════════════════════════════════════════════════════════
# 8. Cancellation mid-chunking
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_cancels_mid_chunking_when_soft_deleted(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fichier soft-deleted entre deux batches embed → skip 'cancelled_midway'."""
    from app.config import settings
    from app.core.storage.text_extractor import ExtractedText

    monkeypatch.setattr(settings, "documents_embed_batch_size", 3)

    row = _make_uploaded_file(mime="text/plain")
    mocks = patch_worker(file_row=row)

    def _fake_chunker(*args, **kwargs):
        return [
            Chunk(
                index=i,
                content=f"chunk {i}",
                token_count=10,
                start_char_offset=i * 10,
                end_char_offset=(i + 1) * 10,
                page_number=None,
            )
            for i in range(9)
        ]

    monkeypatch.setattr(chunk_tasks, "chunk_text", _fake_chunker)
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text="x" * 1000, page_count=None, truncated=False, status="ok"
        ),
    )

    # Monkey-patch _is_file_cancelled pour renvoyer True dès le 2ᵉ check.
    call_count = {"n": 0}

    async def _fake_cancel(_file_uuid):
        call_count["n"] += 1
        return True  # soft-deleted dès la 1ère vérification mid-way

    monkeypatch.setattr(chunk_tasks, "_is_file_cancelled", _fake_cancel)

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result["skipped"] is True
    assert result["reason"] == "cancelled_midway"


# ══════════════════════════════════════════════════════════════
# 9. Extraction pas OK
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_skips_when_extraction_empty(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction 'empty' ou 'failed' → skip + sentinelle posée."""
    from app.core.storage.text_extractor import ExtractedText

    row = _make_uploaded_file(mime="application/pdf")
    mocks = patch_worker(file_row=row)
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text="", page_count=0, truncated=False, status="empty"
        ),
    )

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    assert result["skipped"] is True
    assert "extraction_" in result["reason"]


# ══════════════════════════════════════════════════════════════
# 10. Enqueue fail-silent
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_enqueue_chunking_is_fail_silent_on_redis_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si le pool arq raise, l'enqueue ne doit jamais remonter d'exception."""

    async def _broken_pool():
        raise RuntimeError("Redis connection refused")

    monkeypatch.setattr(chunk_tasks, "_get_arq_pool", _broken_pool)
    # Ne doit pas raise.
    await chunk_tasks.enqueue_chunking(_FILE_ID)


# ══════════════════════════════════════════════════════════════
# 11. Event forensic contient toutes les clés
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_forensic_event_payload_contains_all_keys(
    patch_worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.storage.text_extractor import ExtractedText

    row = _make_uploaded_file(mime="text/plain")
    mocks = patch_worker(file_row=row)

    def _fake_chunker(*args, **kwargs):
        return [
            Chunk(
                index=0,
                content="single chunk",
                token_count=5,
                start_char_offset=0,
                end_char_offset=12,
                page_number=None,
            ),
        ]

    monkeypatch.setattr(chunk_tasks, "chunk_text", _fake_chunker)
    monkeypatch.setattr(
        chunk_tasks,
        "extract_text",
        lambda *args, **kwargs: ExtractedText(
            text="x" * 500, page_count=None, truncated=False, status="ok"
        ),
    )

    result = await chunk_tasks.index_document_chunks({}, str(_FILE_ID))
    # Les clés forensiques attendues sont toutes dans le payload retourné.
    for key in (
        "skipped",
        "n_chunks",
        "total_tokens",
        "embeddings_cost_usd",
        "duration_ms",
        "truncated",
        "pages",
    ):
        assert key in result
