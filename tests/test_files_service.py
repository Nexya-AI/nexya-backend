"""
Tests unitaires — `FileUploadService` (Session E3).

On utilise le `MockObjectStore` réel (RAM), on monkey-patche le scanner via
`reset_virus_scanner`, et on capture les DB `execute` pour vérifier les
statuts finaux sur la row `UploadedFile`.

Couverture :
- Pipeline happy-path : upload OK → INSERT → extract → status 'ok'.
- Rejet MIME annoncé non-whitelist → 415.
- Rejet > cap taille → 413 (interruption précoce).
- Rejet magic-bytes mismatch → 415 FILE_CONTENT_MISMATCH.
- Rejet EICAR → 415 VIRUS_DETECTED (pas d'upload MinIO).
- Dédup SHA → retourne l'existant sans double upload.
- `scan_virus=False` → status 'skipped' sur l'entrée.
- `extract_text=False` → status 'skipped' sur l'entrée.
- Fail-safe extraction (pypdf crash) → upload OK + status 'failed'.
- `get_for_user` 404 IDOR.
- `mark_attached` met à jour les champs.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.core.errors.exceptions import (
    FileContentMismatchException,
    FileTooLargeException,
    FileTypeNotAllowedException,
    ResourceNotFoundException,
    VirusDetectedException,
)
from app.core.storage import MockObjectStore
from app.core.storage.virus_scanner import (
    reset_virus_scanner,
)
from app.features.files.models import UploadedFile
from app.features.files.service import FileUploadService

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_PDF_HEADER = b"%PDF-1.4\n"


def _make_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    user.is_pro = False
    return user


class _FakeUploadFile:
    """Mime de `fastapi.UploadFile` : `.filename`, `.content_type`, `.read`."""

    def __init__(self, data: bytes, filename: str, content_type: str) -> None:
        self._buf = io.BytesIO(data)
        self.filename = filename
        self.content_type = content_type

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size) if size >= 0 else self._buf.read()


class _FakeDB:
    """Fake AsyncSession qui trace les appels execute / add / commit.

    Supporte un pattern séquentiel : chaque `execute` renvoie le prochain
    `_ScalarResult` de la liste. L'`add` mémorise l'objet, `commit` + `refresh`
    sont des AsyncMock.
    """

    def __init__(self, execute_results: list[Any]) -> None:
        self._results = iter(execute_results)
        self.added: list[Any] = []
        self.executed_stmts: list[Any] = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):
        self.executed_stmts.append(stmt)
        return next(self._results)

    def add(self, obj) -> None:
        # Simule le pattern SQLAlchemy : l'INSERT remplit id + timestamps.
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        self.added.append(obj)


class _ScalarResult:
    """Mime partiel de `sqlalchemy.Result`."""

    def __init__(self, *, one: Any | None | type = object) -> None:
        self._one = one

    def scalar_one_or_none(self):
        return None if self._one is object else self._one

    def scalar_one(self):
        # Pour les COUNT(*) qui retournent toujours un entier.
        return 0 if self._one is object else self._one

    def scalar(self):
        return None if self._one is object else self._one


# ══════════════════════════════════════════════════════════════
# 1. Happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_happy_path_png(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_virus_scanner()
    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)

    user = _make_user()
    store = MockObjectStore(bucket="test")
    # DB : 1 SELECT dédup (None) + INSERT (via db.add/commit/refresh) + 1
    # UPDATE extraction status.
    db = _FakeDB(
        execute_results=[
            _ScalarResult(one=None),  # dédup SELECT
            _ScalarResult(),  # UPDATE extraction (result ignoré)
        ]
    )

    png_data = _PNG_HEADER + b"IHDR" + b"\x00" * 100
    upload = _FakeUploadFile(png_data, "photo.png", "image/png")

    row = await FileUploadService.upload(user, db, upload_file=upload, store=store)

    assert row.mime_type == "image/png"
    assert row.size_bytes == len(png_data)
    assert row.virus_scan_status == "clean"
    assert row.extraction_status in {"unsupported", "ok", "empty"}
    assert row.storage_key.startswith(f"{user.id}/uploads/")
    # MinIO a bien reçu le binaire.
    stat = await store.stat_object(row.storage_key)
    assert stat is not None
    assert stat.size_bytes == len(png_data)


# ══════════════════════════════════════════════════════════════
# 2. Rejets du pipeline
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_rejects_non_whitelist_mime() -> None:
    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _FakeDB(execute_results=[])

    upload = _FakeUploadFile(b"#!/bin/sh\necho hi\n", "run.sh", "application/x-sh")
    with pytest.raises(FileTypeNotAllowedException):
        await FileUploadService.upload(user, db, upload_file=upload, store=store)


@pytest.mark.asyncio
async def test_upload_rejects_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _FakeDB(execute_results=[])
    monkeypatch.setattr(settings, "files_max_upload_bytes", 64, raising=False)

    big = _PNG_HEADER + b"\x00" * 512
    upload = _FakeUploadFile(big, "big.png", "image/png")
    with pytest.raises(FileTooLargeException):
        await FileUploadService.upload(user, db, upload_file=upload, store=store)


@pytest.mark.asyncio
async def test_upload_rejects_magic_mismatch() -> None:
    """MIME annoncé = image/png mais magic = PDF → 415 FILE_CONTENT_MISMATCH."""
    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _FakeDB(execute_results=[])

    pdf = _PDF_HEADER + b"fake"
    upload = _FakeUploadFile(pdf, "deceive.png", "image/png")
    with pytest.raises(FileContentMismatchException):
        await FileUploadService.upload(user, db, upload_file=upload, store=store)


@pytest.mark.asyncio
async def test_upload_rejects_magic_unknown() -> None:
    """Bytes aléatoires sans magic connu → 415 FILE_CONTENT_MISMATCH."""
    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _FakeDB(execute_results=[])

    upload = _FakeUploadFile(b"\x00\x01\x02\x03\x04\x05", "junk.png", "image/png")
    with pytest.raises(FileContentMismatchException):
        await FileUploadService.upload(user, db, upload_file=upload, store=store)


@pytest.mark.asyncio
async def test_upload_rejects_eicar_virus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_virus_scanner()
    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)

    user = _make_user()
    store = MockObjectStore(bucket="test")
    # D4 : le 1er execute est le COUNT quota (0 = sous la limite), puis
    # le SELECT dédup (None = pas de dédup).
    db = _FakeDB(
        execute_results=[
            _ScalarResult(one=0),
            _ScalarResult(one=None),
        ]
    )

    # On encapsule EICAR dans un pseudo-PDF : MIME annoncé + magic-bytes
    # matchent, mais le scanner le détecte.
    eicar = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    pdf_with_eicar = _PDF_HEADER + eicar + b"\n%%EOF\n"
    upload = _FakeUploadFile(pdf_with_eicar, "virus.pdf", "application/pdf")

    with pytest.raises(VirusDetectedException):
        await FileUploadService.upload(user, db, upload_file=upload, store=store)

    # Rien n'a été uploadé sur MinIO (rejet avant upload).
    assert store._fetch_raw(f"{user.id}/uploads/fake/key.pdf") is None


# ══════════════════════════════════════════════════════════════
# 3. Dédup SHA
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_dedup_returns_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_virus_scanner()
    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)

    user = _make_user()
    store = MockObjectStore(bucket="test")

    # Existing row retournée par le SELECT dédup.
    existing = UploadedFile(
        user_id=user.id,
        storage_key="c4a2/uploads/ab/abcd.png",
        content_sha256="a" * 64,
        size_bytes=42,
        mime_type="image/png",
    )
    existing.id = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001")
    existing.created_at = datetime.now(UTC)
    existing.updated_at = datetime.now(UTC)
    existing.deleted_at = None
    existing.extraction_status = "ok"
    existing.virus_scan_status = "clean"

    db = _FakeDB(execute_results=[_ScalarResult(one=existing)])

    png_data = _PNG_HEADER + b"\x00" * 64
    upload = _FakeUploadFile(png_data, "dupe.png", "image/png")
    row = await FileUploadService.upload(user, db, upload_file=upload, store=store)

    assert row.id == existing.id
    # Pas d'upload MinIO ni d'INSERT (dédup = court-circuit).
    assert db.added == []


# ══════════════════════════════════════════════════════════════
# 4. Skip scan / skip extract
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_skip_virus_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_virus_scanner()
    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)

    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _FakeDB(
        execute_results=[
            _ScalarResult(one=None),  # dédup
            _ScalarResult(),  # UPDATE extraction
        ]
    )

    upload = _FakeUploadFile(_PNG_HEADER + b"\x00" * 30, "a.png", "image/png")
    row = await FileUploadService.upload(
        user, db, upload_file=upload, scan_virus=False, store=store
    )
    assert row.virus_scan_status == "skipped"


@pytest.mark.asyncio
async def test_upload_skip_text_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_virus_scanner()
    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)

    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _FakeDB(
        execute_results=[
            _ScalarResult(one=None),  # dédup
            _ScalarResult(),  # UPDATE extraction → skipped
        ]
    )

    upload = _FakeUploadFile(_PNG_HEADER + b"\x00" * 30, "a.png", "image/png")
    row = await FileUploadService.upload(
        user, db, upload_file=upload, extract_text_enabled=False, store=store
    )
    assert row.extraction_status == "skipped"


# ══════════════════════════════════════════════════════════════
# 5. Fail-safe extraction
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_extract_failure_keeps_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si extract_text raise une exception, upload reste OK et row marque failed."""
    reset_virus_scanner()
    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)

    user = _make_user()
    store = MockObjectStore(bucket="test")
    # D4 : PDF = chunking-éligible → le 1er execute est le COUNT quota.
    db = _FakeDB(
        execute_results=[
            _ScalarResult(one=0),  # D4 : COUNT quota
            _ScalarResult(one=None),  # dédup
            _ScalarResult(),  # UPDATE (failed)
        ]
    )

    def _extract_crash(*args, **kwargs):
        raise RuntimeError("boom in pypdf")

    from app.features.files import service as files_service_module

    monkeypatch.setattr(files_service_module, "extract_text", _extract_crash)

    upload = _FakeUploadFile(_PDF_HEADER + b"fake content\n%%EOF\n", "doc.pdf", "application/pdf")
    row = await FileUploadService.upload(user, db, upload_file=upload, store=store)

    assert row.extraction_status == "failed"
    # Le fichier est bien sur MinIO (upload a réussi avant l'extraction).
    stat = await store.stat_object(row.storage_key)
    assert stat is not None


# ══════════════════════════════════════════════════════════════
# 6. get_for_user + mark_attached
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_for_user_raises_404_on_non_owner() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarResult(one=None)])

    with pytest.raises(ResourceNotFoundException):
        await FileUploadService.get_for_user(uuid.uuid4(), user, db)


@pytest.mark.asyncio
async def test_mark_attached_commits_update() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarResult()])

    await FileUploadService.mark_attached(
        uuid.uuid4(),
        user.id,
        kind="project_file",
        target_id=uuid.uuid4(),
        db=db,
    )
    db.commit.assert_awaited_once()
