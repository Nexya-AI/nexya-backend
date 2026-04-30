"""
Tests d'intégration — router `/files/upload` (Session E3).

TestClient FastAPI + guards surchargés + `FileUploadService.upload`
monkeypatché. On vérifie :
- 201 + enveloppe NexyaResponse + presigned URL + preview texte.
- 415 FILE_TYPE_NOT_ALLOWED / FILE_CONTENT_MISMATCH / VIRUS_DETECTED.
- 413 FILE_TOO_LARGE.
- 429 RATE_LIMIT_ABUSE (après 20 uploads/heure).
- Query params `extract_text` / `scan_virus` forwardés au service.
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
    FileContentMismatchException,
    FileTooLargeException,
    FileTypeNotAllowedException,
    RateLimitAbuseException,
    VirusDetectedException,
)
from app.features.auth.models import User
from app.features.files.models import UploadedFile
from app.features.files.service import FileUploadService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_URL = "mock://nexya-media/uploads/fake.url?expires=123"


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_upload(*, upload_id: uuid.UUID | None = None) -> UploadedFile:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    row = UploadedFile(
        user_id=_FAKE_USER_ID,
        storage_key="c4a2/uploads/ab/abcd.pdf",
        content_sha256="a" * 64,
        size_bytes=4096,
        mime_type="application/pdf",
        original_filename="rapport.pdf",
        extension="pdf",
        virus_scan_status="clean",
        virus_scan_signature=None,
        virus_scan_scanner="mock",
        extraction_status="ok",
    )
    row.id = upload_id or uuid.UUID("11111111-0000-4000-8000-000000000001")
    row.created_at = now
    row.updated_at = now
    row.deleted_at = None
    row.virus_scanned_at = now
    row.extracted_text = "Texte extrait du PDF. " * 5
    row.extracted_text_length = len(row.extracted_text)
    row.page_count = 3
    row.extraction_truncated = False
    row.extracted_at = now
    row.attached_to_kind = None
    row.attached_to_id = None
    row.attached_at = None
    return row


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    # Bypass du rate limiter — testé séparément.
    from app.core.security import rate_limiter as rl_module

    monkeypatch.setattr(rl_module, "check_user_rate_limit", AsyncMock(return_value=None))
    from app.features.files import router as files_router_module

    monkeypatch.setattr(files_router_module, "check_user_rate_limit", AsyncMock(return_value=None))

    monkeypatch.setattr(
        FileUploadService,
        "presigned_url_for",
        AsyncMock(return_value=_FAKE_URL),
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


def test_upload_returns_201_with_envelope_and_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _make_fake_upload()
    monkeypatch.setattr(FileUploadService, "upload", AsyncMock(return_value=row))

    response = client.post(
        "/files/upload",
        files={"file": ("rapport.pdf", b"%PDF-1.4\nfake", "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(row.id)
    assert body["data"]["url"] == _FAKE_URL
    assert body["data"]["mime_type"] == "application/pdf"
    assert body["data"]["virus_scan_status"] == "clean"
    assert body["data"]["extraction_status"] == "ok"
    assert body["data"]["extracted_text_preview"] is not None
    # Preview ≤ 500 chars.
    assert len(body["data"]["extracted_text_preview"]) <= 500


# ══════════════════════════════════════════════════════════════
# 2. Rejets — codes d'erreur
# ══════════════════════════════════════════════════════════════


def test_upload_returns_415_on_mime_not_allowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FileUploadService,
        "upload",
        AsyncMock(side_effect=FileTypeNotAllowedException(mime_type="application/x-sh")),
    )
    response = client.post(
        "/files/upload",
        files={"file": ("r.sh", b"#!/bin/sh", "application/x-sh")},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "FILE_TYPE_NOT_ALLOWED"


def test_upload_returns_415_on_content_mismatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FileUploadService,
        "upload",
        AsyncMock(
            side_effect=FileContentMismatchException(
                announced="image/png", detected="application/pdf"
            )
        ),
    )
    response = client.post(
        "/files/upload",
        files={"file": ("x.png", b"%PDF-1.4\nfake", "image/png")},
    )
    assert response.status_code == 415
    body = response.json()
    assert body["code"] == "FILE_CONTENT_MISMATCH"
    assert body["data"]["announced"] == "image/png"
    assert body["data"]["detected"] == "application/pdf"


def test_upload_returns_415_on_virus_detected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FileUploadService,
        "upload",
        AsyncMock(
            side_effect=VirusDetectedException(signature="EICAR-TEST-SIGNATURE", scanner="mock")
        ),
    )
    response = client.post(
        "/files/upload",
        files={"file": ("v.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 415
    body = response.json()
    assert body["code"] == "VIRUS_DETECTED"
    assert body["data"]["signature"] == "EICAR-TEST-SIGNATURE"


def test_upload_returns_413_on_too_large(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        FileUploadService,
        "upload",
        AsyncMock(side_effect=FileTooLargeException(max_mb=100)),
    )
    response = client.post(
        "/files/upload",
        files={"file": ("big.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"


def test_upload_returns_429_on_rate_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rate limiter activé → 429 avant même d'appeler le service."""
    from app.features.files import router as files_router_module

    monkeypatch.setattr(
        files_router_module,
        "check_user_rate_limit",
        AsyncMock(side_effect=RateLimitAbuseException(retry_after=1800)),
    )
    response = client.post(
        "/files/upload",
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 429
    assert response.json()["code"] == "RATE_LIMIT_ABUSE"


def test_upload_reaches_service_only_with_valid_multipart(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check : le service est bien appelé sur un multipart valide,
    et le router retourne 201. Ce test vaut implicitement validation du
    chemin heureux minimal (les cas d'erreur d'entrée sont couverts par
    les tests mime/size/virus/content-mismatch ci-dessus)."""
    row = _make_fake_upload()
    mock_upload = AsyncMock(return_value=row)
    monkeypatch.setattr(FileUploadService, "upload", mock_upload)

    response = client.post(
        "/files/upload",
        files={"file": ("doc.pdf", b"%PDF-1.4\n", "application/pdf")},
    )
    assert response.status_code == 201
    mock_upload.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 3. Query params forwarding
# ══════════════════════════════════════════════════════════════


def test_upload_forwards_extract_text_and_scan_virus_flags(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _make_fake_upload()
    mock_upload = AsyncMock(return_value=row)
    monkeypatch.setattr(FileUploadService, "upload", mock_upload)

    response = client.post(
        "/files/upload?extract_text=false&scan_virus=false",
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 201
    kwargs = mock_upload.await_args.kwargs
    assert kwargs["extract_text_enabled"] is False
    assert kwargs["scan_virus"] is False
