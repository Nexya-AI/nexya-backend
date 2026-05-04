"""
Tests unitaires — enrichissement `ProjectFileResponse` (Session D2.5,
2026-05-04).

Cible les 2 helpers de `app/features/projects/router.py` :

- `_project_file_to_response(pfile, db)` (single — utilisé par
  `POST /projects/{id}/files`) : signe la presigned URL si `storage_key`
  présent + recopie `chunks_indexed_at` depuis la row `UploadedFile`
  rattachée via le lien polymorphe `attached_to_kind/_id`.
- `_project_files_to_responses(pfiles, db)` (bulk — utilisé par
  `GET /projects/{id}/files`) : 1 SEULE requête bulk
  `WHERE attached_to_id IN (...)` pour rapatrier tous les UploadedFile
  attachés (anti-N+1), puis N appels presigned URL locaux.

Couvre :
- presigned_url non-null si `storage_key` non-null, None sinon (mode
  legacy C2 pur).
- chunks_indexed_at recopié depuis l'UploadedFile attaché — None si pas
  d'upload rattaché (mode legacy) ou si le worker D4 n'a pas encore
  posé la sentinelle.
- bulk anti-N+1 : pour 10 ProjectFile, `db.execute` est appelé exactement
  1 fois (pas 10), vérifié via `await_count`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.files.models import UploadedFile
from app.features.projects.models import ProjectFile
from app.features.projects.router import (
    _project_file_to_response,
    _project_files_to_responses,
)

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_PROJECT_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
_FAKE_PRESIGNED_URL = "mock://nexya-media/uploads/abcd.pdf?expires=999&sig=xxx"


def _make_fake_pfile(
    *,
    pfile_id: uuid.UUID | None = None,
    storage_key: str | None = "c4a2/uploads/ab/abcd.pdf",
) -> ProjectFile:
    now = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    pf = ProjectFile(
        project_id=_FAKE_PROJECT_ID,
        name="rapport.pdf",
        file_type="pdf",
    )
    pf.id = pfile_id or uuid.uuid4()
    pf.storage_key = storage_key
    pf.size_bytes = 4096 if storage_key else None
    pf.mime_type = "application/pdf" if storage_key else None
    pf.created_at = now
    pf.updated_at = now
    pf.uploaded_at = now
    pf.deleted_at = None
    return pf


def _make_fake_upload(
    *,
    upload_id: uuid.UUID | None = None,
    attached_to_id: uuid.UUID | None = None,
    chunks_indexed_at: datetime | None = None,
) -> UploadedFile:
    now = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    u = UploadedFile(
        user_id=_FAKE_USER_ID,
        storage_key="c4a2/uploads/ab/abcd.pdf",
        content_sha256="a" * 64,
        size_bytes=4096,
        mime_type="application/pdf",
    )
    u.id = upload_id or uuid.uuid4()
    u.created_at = now
    u.updated_at = now
    u.deleted_at = None
    u.attached_to_kind = "project_file" if attached_to_id else None
    u.attached_to_id = attached_to_id
    u.attached_at = now if attached_to_id else None
    u.chunks_indexed_at = chunks_indexed_at
    return u


def _make_db_returning_uploads(uploads: list[UploadedFile]) -> MagicMock:
    """Construit un fake AsyncSession dont `execute` retourne un Result
    qui yield les `uploads` via `.scalars().all()`."""
    db = MagicMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=uploads)
    result.scalars = MagicMock(return_value=scalars)
    # scalar_one_or_none() pour le single helper (1er upload de la liste ou None).
    result.scalar_one_or_none = MagicMock(
        return_value=uploads[0] if uploads else None
    )
    db.execute = AsyncMock(return_value=result)
    return db


def _patch_object_store(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Mocke `get_object_store()` pour retourner un store dont
    `generate_presigned_url` est un AsyncMock retournant une URL fake.
    Retourne le mock pour permettre l'inspection du nombre d'appels."""
    presigned_mock = AsyncMock(return_value=_FAKE_PRESIGNED_URL)
    fake_store = MagicMock()
    fake_store.generate_presigned_url = presigned_mock
    monkeypatch.setattr(
        "app.core.storage.get_object_store",
        lambda: fake_store,
    )
    return presigned_mock


# ══════════════════════════════════════════════════════════════
# 1. Single helper — `_project_file_to_response`
# ══════════════════════════════════════════════════════════════


def test_single_helper_signs_presigned_url_when_storage_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    presigned_mock = _patch_object_store(monkeypatch)
    pfile = _make_fake_pfile()
    upload = _make_fake_upload(
        attached_to_id=pfile.id,
        chunks_indexed_at=datetime(2026, 5, 4, 10, 30, 0, tzinfo=UTC),
    )
    db = _make_db_returning_uploads([upload])

    response = asyncio.run(_project_file_to_response(pfile, db))

    assert response.presigned_url == _FAKE_PRESIGNED_URL
    assert response.upload_id == upload.id
    assert response.chunks_indexed_at == upload.chunks_indexed_at
    presigned_mock.assert_awaited_once()


def test_single_helper_returns_none_presigned_when_no_storage_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode legacy C2 pur : `storage_key=None` → pas d'appel presigned,
    presigned_url=None dans la réponse."""
    presigned_mock = _patch_object_store(monkeypatch)
    pfile = _make_fake_pfile(storage_key=None)
    db = _make_db_returning_uploads([])  # pas d'upload rattaché

    response = asyncio.run(_project_file_to_response(pfile, db))

    assert response.presigned_url is None
    assert response.upload_id is None
    assert response.chunks_indexed_at is None
    presigned_mock.assert_not_awaited()


def test_single_helper_chunks_indexed_at_none_when_worker_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload rattaché mais `chunks_indexed_at=None` (worker D4 toujours
    en cours ou MIME non éligible) → réponse expose None, pas une date
    factice."""
    _patch_object_store(monkeypatch)
    pfile = _make_fake_pfile()
    upload = _make_fake_upload(attached_to_id=pfile.id, chunks_indexed_at=None)
    db = _make_db_returning_uploads([upload])

    response = asyncio.run(_project_file_to_response(pfile, db))

    assert response.upload_id == upload.id
    assert response.chunks_indexed_at is None


# ══════════════════════════════════════════════════════════════
# 2. Bulk helper — `_project_files_to_responses` (anti-N+1)
# ══════════════════════════════════════════════════════════════


def test_bulk_helper_returns_empty_list_for_empty_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Court-circuit propre : pas d'appel DB ni store si la liste est vide."""
    presigned_mock = _patch_object_store(monkeypatch)
    db = MagicMock()
    db.execute = AsyncMock()

    result = asyncio.run(_project_files_to_responses([], db))

    assert result == []
    db.execute.assert_not_awaited()
    presigned_mock.assert_not_awaited()


def test_bulk_helper_uses_single_query_for_n_pfiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-N+1 strict : pour 10 ProjectFile, `db.execute` est appelé
    exactement 1 fois (la requête bulk `WHERE attached_to_id IN (...)`)."""
    _patch_object_store(monkeypatch)
    pfiles = [_make_fake_pfile(pfile_id=uuid.uuid4()) for _ in range(10)]
    # Aucun upload rattaché — on teste juste le compte d'appels DB.
    db = _make_db_returning_uploads([])

    result = asyncio.run(_project_files_to_responses(pfiles, db))

    assert len(result) == 10
    # Une seule requête DB pour rapatrier les 10 UploadedFile potentiels.
    assert db.execute.await_count == 1


def test_bulk_helper_maps_uploads_to_pfiles_by_attached_to_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le mapping `uploads_by_target[u.attached_to_id] = u` doit retrouver
    le bon UploadedFile pour chaque ProjectFile, et copier
    `chunks_indexed_at` correctement (pas de cross-contamination)."""
    _patch_object_store(monkeypatch)
    pfiles = [_make_fake_pfile(pfile_id=uuid.uuid4()) for _ in range(3)]
    indexed_at_0 = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
    indexed_at_2 = datetime(2026, 5, 4, 11, 0, 0, tzinfo=UTC)
    uploads = [
        _make_fake_upload(attached_to_id=pfiles[0].id, chunks_indexed_at=indexed_at_0),
        # pfiles[1] : pas d'upload rattaché.
        _make_fake_upload(attached_to_id=pfiles[2].id, chunks_indexed_at=indexed_at_2),
    ]
    db = _make_db_returning_uploads(uploads)

    result = asyncio.run(_project_files_to_responses(pfiles, db))

    assert len(result) == 3
    # pfiles[0] → upload 0 → chunks_indexed_at_0.
    assert result[0].upload_id == uploads[0].id
    assert result[0].chunks_indexed_at == indexed_at_0
    # pfiles[1] → pas d'upload → tous les enrichissements à None.
    assert result[1].upload_id is None
    assert result[1].chunks_indexed_at is None
    # pfiles[2] → upload 1 (2ᵉ row) → chunks_indexed_at_2.
    assert result[2].upload_id == uploads[1].id
    assert result[2].chunks_indexed_at == indexed_at_2


def test_bulk_helper_signs_presigned_url_per_pfile_with_storage_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N appels presigned (un par ProjectFile avec storage_key non-null,
    impossible à bulker côté MinIO mais ~1 ms chacun en local)."""
    presigned_mock = _patch_object_store(monkeypatch)
    pfiles = [
        _make_fake_pfile(pfile_id=uuid.uuid4(), storage_key="key-with"),
        _make_fake_pfile(pfile_id=uuid.uuid4(), storage_key=None),  # mode legacy
        _make_fake_pfile(pfile_id=uuid.uuid4(), storage_key="key-with-2"),
    ]
    db = _make_db_returning_uploads([])

    result = asyncio.run(_project_files_to_responses(pfiles, db))

    assert len(result) == 3
    assert result[0].presigned_url == _FAKE_PRESIGNED_URL
    assert result[1].presigned_url is None  # storage_key=None → pas d'appel
    assert result[2].presigned_url == _FAKE_PRESIGNED_URL
    # 2 appels presigned (le pfile sans storage_key est skippé).
    assert presigned_mock.await_count == 2
