"""Tests du download proxy authentifié (fix P0 2026-06-10).

Couvre le remplacement du presigned MinIO (injoignable depuis le téléphone)
par `GET /generate/document/download/{library_id}` qui stream le binaire depuis
MinIO interne via le backend.

Mock-first strict :
    - `build_document_download_path` : pur (aucune dépendance).
    - `DocumentGeneratorService.fetch_for_download` : LibraryService.get +
      get_object_store monkeypatchés (pas de DB, pas de MinIO réel).
    - Endpoint : fetch_for_download monkeypatché + DB/auth via dependency_overrides.
    - Async : `DocumentJobService.to_response` renvoie le chemin relatif.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import ResourceNotFoundException
from app.features.document_generator import router as router_module
from app.features.document_generator import service as service_module
from app.features.document_generator.download import (
    DOCUMENT_DOWNLOAD_ROUTE,
    build_document_download_path,
)
from app.features.document_generator.exceptions import DocumentStorageUnavailableError
from app.features.document_generator.job_service import DocumentJobService
from app.features.document_generator.service import DocumentGeneratorService
from app.features.library.service import LibraryService
from app.main import app

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_fake_user(is_pro: bool = False):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_pro = is_pro
    return user


def _make_fake_item(*, type_: str = "document", file_type: str = "pdf"):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.type = type_
    item.file_type = file_type
    item.mime_type = "application/pdf"
    item.title = "Mon document"
    item.storage_key = "user/library/document/ab/abcd.pdf"
    return item


def _install_overrides(monkeypatch: pytest.MonkeyPatch, user, *, skip_rate_limit: bool = True):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    if skip_rate_limit:

        async def fake_rate_limit(*args, **kwargs):
            return None

        monkeypatch.setattr(router_module, "check_user_rate_limit", fake_rate_limit)


def _cleanup_overrides():
    app.dependency_overrides.clear()


# ──────────────────────────────────────────────────────────────────
# build_document_download_path (pur)
# ──────────────────────────────────────────────────────────────────


class TestBuildDownloadPath:
    def test_returns_relative_path_with_library_id(self) -> None:
        lib_id = uuid.uuid4()
        path = build_document_download_path(lib_id)
        assert path == f"{DOCUMENT_DOWNLOAD_ROUTE}/{lib_id}"

    def test_is_relative_not_absolute(self) -> None:
        # Le chemin DOIT être relatif : le dio Flutter le résout contre baseUrl.
        path = build_document_download_path(uuid.uuid4())
        assert path.startswith("/generate/document/download/")
        assert "minio" not in path
        assert "http" not in path

    def test_route_constant_aligned(self) -> None:
        assert DOCUMENT_DOWNLOAD_ROUTE == "/generate/document/download"


# ──────────────────────────────────────────────────────────────────
# DocumentGeneratorService.fetch_for_download
# ──────────────────────────────────────────────────────────────────


class TestFetchForDownload:
    @pytest.mark.asyncio
    async def test_happy_returns_bytes_mime_filename(self, monkeypatch: pytest.MonkeyPatch) -> None:
        item = _make_fake_item()
        monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))

        fake_store = MagicMock()
        fake_store.download_bytes = AsyncMock(return_value=b"%PDF-1.4 fake bytes")
        monkeypatch.setattr(service_module, "get_object_store", lambda: fake_store)

        data, mime, filename = await DocumentGeneratorService.fetch_for_download(
            item.id, _make_fake_user(), MagicMock()
        )

        assert data == b"%PDF-1.4 fake bytes"
        assert mime == "application/pdf"
        assert filename.endswith(".pdf")
        fake_store.download_bytes.assert_awaited_once_with(item.storage_key)

    @pytest.mark.asyncio
    async def test_owner_check_delegates_to_library_get(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = _make_fake_item()
        lib_get = AsyncMock(return_value=item)
        monkeypatch.setattr(LibraryService, "get", lib_get)
        fake_store = MagicMock()
        fake_store.download_bytes = AsyncMock(return_value=b"x")
        monkeypatch.setattr(service_module, "get_object_store", lambda: fake_store)

        user = _make_fake_user()
        db = MagicMock()
        await DocumentGeneratorService.fetch_for_download(item.id, user, db)

        # IDOR-safe : l'owner-check passe par LibraryService.get(id, user, db).
        lib_get.assert_awaited_once_with(item.id, user, db)

    @pytest.mark.asyncio
    async def test_non_document_type_raises_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Un item owned mais de type image/audio ne passe PAS par cet endpoint.
        item = _make_fake_item(type_="image", file_type=None)
        monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))
        store_factory = MagicMock()  # ne doit jamais être appelé
        monkeypatch.setattr(service_module, "get_object_store", store_factory)

        with pytest.raises(ResourceNotFoundException):
            await DocumentGeneratorService.fetch_for_download(
                item.id, _make_fake_user(), MagicMock()
            )
        store_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_idor_propagates_library_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # LibraryService.get lève 404 (pas owned / soft-deleted) → propagé tel quel.
        monkeypatch.setattr(
            LibraryService,
            "get",
            AsyncMock(side_effect=ResourceNotFoundException("Document")),
        )
        with pytest.raises(ResourceNotFoundException):
            await DocumentGeneratorService.fetch_for_download(
                uuid.uuid4(), _make_fake_user(), MagicMock()
            )

    @pytest.mark.asyncio
    async def test_blob_missing_raises_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        item = _make_fake_item()
        monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))
        fake_store = MagicMock()
        fake_store.download_bytes = AsyncMock(side_effect=FileNotFoundError("gone"))
        monkeypatch.setattr(service_module, "get_object_store", lambda: fake_store)

        with pytest.raises(ResourceNotFoundException):
            await DocumentGeneratorService.fetch_for_download(
                item.id, _make_fake_user(), MagicMock()
            )

    @pytest.mark.asyncio
    async def test_storage_down_raises_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        item = _make_fake_item()
        monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))
        fake_store = MagicMock()
        fake_store.download_bytes = AsyncMock(side_effect=RuntimeError("minio down"))
        monkeypatch.setattr(service_module, "get_object_store", lambda: fake_store)

        with pytest.raises(DocumentStorageUnavailableError):
            await DocumentGeneratorService.fetch_for_download(
                item.id, _make_fake_user(), MagicMock()
            )


# ──────────────────────────────────────────────────────────────────
# Endpoint GET /generate/document/download/{library_id}
# ──────────────────────────────────────────────────────────────────


class TestDownloadEndpoint:
    def test_200_streams_bytes_with_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)
        lib_id = uuid.uuid4()

        async def fake_fetch(library_id, current_user, db):
            return (b"%PDF-1.4 hello", "application/pdf", "mon_document.pdf")

        monkeypatch.setattr(DocumentGeneratorService, "fetch_for_download", fake_fetch)
        try:
            with TestClient(app) as client:
                resp = client.get(f"/generate/document/download/{lib_id}")
            assert resp.status_code == 200
            assert resp.content == b"%PDF-1.4 hello"
            assert resp.headers["content-type"].startswith("application/pdf")
            assert resp.headers["content-length"] == str(len(b"%PDF-1.4 hello"))
            assert "mon_document.pdf" in resp.headers["content-disposition"]
        finally:
            _cleanup_overrides()

    def test_404_idor_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        async def fake_fetch(library_id, current_user, db):
            raise ResourceNotFoundException("Document")

        monkeypatch.setattr(DocumentGeneratorService, "fetch_for_download", fake_fetch)
        try:
            with TestClient(app) as client:
                resp = client.get(f"/generate/document/download/{uuid.uuid4()}")
            assert resp.status_code == 404
            assert resp.json()["code"] == "RESOURCE_NOT_FOUND"
        finally:
            _cleanup_overrides()

    def test_503_storage_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        async def fake_fetch(library_id, current_user, db):
            raise DocumentStorageUnavailableError("minio down")

        monkeypatch.setattr(DocumentGeneratorService, "fetch_for_download", fake_fetch)
        try:
            with TestClient(app) as client:
                resp = client.get(f"/generate/document/download/{uuid.uuid4()}")
            assert resp.status_code == 503
        finally:
            _cleanup_overrides()

    def test_401_without_auth(self) -> None:
        # Aucun override d'auth → guard rejette.
        with TestClient(app) as client:
            resp = client.get(f"/generate/document/download/{uuid.uuid4()}")
        assert resp.status_code in (401, 403)

    def test_422_malformed_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)
        try:
            with TestClient(app) as client:
                resp = client.get("/generate/document/download/not-a-uuid")
            assert resp.status_code == 422
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# Async path : DocumentJobService.to_response renvoie le chemin relatif
# ──────────────────────────────────────────────────────────────────


def _make_fake_job(*, status: str, library_id: uuid.UUID | None):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.status = status
    job.format = "pdf"
    job.template = "minimal"
    job.library_id = library_id
    job.filename = "doc.pdf"
    job.pages = 3
    job.size_bytes = 1234
    job.truncated = False
    job.error_code = None
    job.created_at = datetime.now(UTC)
    job.completed_at = datetime.now(UTC)
    return job


class TestAsyncJobDownloadUrl:
    @pytest.mark.asyncio
    async def test_done_job_returns_relative_path(self) -> None:
        lib_id = uuid.uuid4()
        job = _make_fake_job(status="done", library_id=lib_id)
        resp = await DocumentJobService.to_response(job, _make_fake_user(), MagicMock())
        assert resp.download_url == build_document_download_path(lib_id)

    @pytest.mark.asyncio
    async def test_pending_job_has_no_download_url(self) -> None:
        job = _make_fake_job(status="processing", library_id=None)
        resp = await DocumentJobService.to_response(job, _make_fake_user(), MagicMock())
        assert resp.download_url is None
