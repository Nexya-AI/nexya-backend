"""
Tests d'intégration — `GET /files/{upload_id}/preview` (C4.10).

Cible la nouvelle route C4.10 qui retourne un StreamingResponse PDF :
- 200 happy path + headers (Content-Type, Cache-Control, X-Preview-Cache)
- 404 IDOR-safe propagé depuis FileUploadService.get_for_user
- 415 FILE_TYPE_NOT_PREVIEWABLE si MIME hors {pdf, docx}
- 429 RATE_LIMIT_ABUSE si > 60 previews/heure
- 503 kill-switch backend off
- 503 pipeline crash
- 401 sans JWT

Pattern strict aligné `test_get_uploaded_file.py` D2.5.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    FilePreviewNotPreviewableException,
    FilePreviewUnavailableException,
    RateLimitAbuseException,
    ResourceNotFoundException,
)
from app.features.auth.models import User
from app.features.files.preview_service import PreviewResult, PreviewService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_UPLOAD_ID = uuid.UUID("11111111-0000-4000-8000-000000000001")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


@pytest.fixture
def client() -> TestClient:
    """Client authentifié — guards surchargés."""
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


@pytest.fixture
def anon_client() -> TestClient:
    """Client SANS auth — utilisé pour vérifier le 401."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch: pytest.MonkeyPatch):
    """Bypass rate limit dans tous les tests sauf celui dédié au 429."""
    from app.features.files import router as files_router

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(files_router, "check_user_rate_limit", _noop)


# ══════════════════════════════════════════════════════════════
# 1. Happy path — 200 + StreamingResponse PDF + headers
# ══════════════════════════════════════════════════════════════


def test_preview_returns_200_with_pdf_content_and_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 + StreamingResponse application/pdf + headers cache + X-Preview-Cache miss."""
    fake_pdf = b"%PDF-1.4\nfake content for test\n%%EOF"
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(
            return_value=PreviewResult(
                pdf_bytes=fake_pdf,
                from_cache=False,
                truncated=False,
                size_bytes=len(fake_pdf),
            )
        ),
    )

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content == fake_pdf
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert response.headers["x-preview-cache"] == "miss"
    # Pas de X-Preview-Truncated quand False
    assert "x-preview-truncated" not in response.headers


def test_preview_cache_hit_returns_hit_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """X-Preview-Cache: hit quand le PDF vient du cache MinIO."""
    fake_pdf = b"%PDF-1.4 cached"
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(
            return_value=PreviewResult(
                pdf_bytes=fake_pdf,
                from_cache=True,
                truncated=False,
                size_bytes=len(fake_pdf),
            )
        ),
    )

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 200
    assert response.headers["x-preview-cache"] == "hit"


def test_preview_truncated_sets_header(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Preview-Truncated: true quand le DOCX a été tronqué."""
    fake_pdf = b"%PDF-1.4 truncated content"
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(
            return_value=PreviewResult(
                pdf_bytes=fake_pdf,
                from_cache=False,
                truncated=True,
                size_bytes=len(fake_pdf),
            )
        ),
    )

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 200
    assert response.headers["x-preview-truncated"] == "true"


# ══════════════════════════════════════════════════════════════
# 2. 404 IDOR-safe
# ══════════════════════════════════════════════════════════════


def test_preview_returns_404_when_upload_not_owned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ResourceNotFoundException propagée → 404 RESOURCE_NOT_FOUND."""
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(side_effect=ResourceNotFoundException("Upload")),
    )

    response = client.get(f"/files/{uuid.uuid4()}/preview")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 3. 415 FILE_TYPE_NOT_PREVIEWABLE
# ══════════════════════════════════════════════════════════════


def test_preview_returns_415_when_mime_not_previewable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MIME hors {pdf, docx} → 415 FILE_TYPE_NOT_PREVIEWABLE."""
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(side_effect=FilePreviewNotPreviewableException(mime_type="audio/mpeg")),
    )

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 415
    body = response.json()
    assert body["code"] == "FILE_TYPE_NOT_PREVIEWABLE"
    assert "audio/mpeg" in body["error"]


# ══════════════════════════════════════════════════════════════
# 4. 503 — kill-switch off
# ══════════════════════════════════════════════════════════════


def test_preview_returns_503_when_killswitch_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill-switch `documents_generator_preview_enabled=False` → 503."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "documents_generator_preview_enabled", False)

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 503


def test_preview_returns_503_when_pipeline_crashes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipeline crash (mammoth + fallback texte échouent) → 503 FILE_PREVIEW_UNAVAILABLE."""
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(side_effect=FilePreviewUnavailableException(reason="weasyprint_timeout")),
    )

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 503
    assert response.json()["code"] == "FILE_PREVIEW_UNAVAILABLE"


# ══════════════════════════════════════════════════════════════
# 5. 429 — rate limit dépassé
# ══════════════════════════════════════════════════════════════


def test_preview_returns_429_when_rate_limit_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """> 60 previews/heure/user → 429 RATE_LIMIT_ABUSE avec retry_after."""
    from app.features.files import router as files_router

    async def _raise_rate_limit(*args, **kwargs):
        raise RateLimitAbuseException(retry_after=3600)

    monkeypatch.setattr(files_router, "check_user_rate_limit", _raise_rate_limit)

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "RATE_LIMIT_ABUSE"
    assert body["data"]["retry_after"] == 3600


# ══════════════════════════════════════════════════════════════
# 6. 422 — UUID malformé
# ══════════════════════════════════════════════════════════════


def test_preview_returns_422_on_malformed_uuid(client: TestClient) -> None:
    """Path non-UUID → 422 Pydantic AVANT toute logique métier."""
    response = client.get("/files/not-a-uuid/preview")
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 7. 401 — pas de JWT
# ══════════════════════════════════════════════════════════════


def test_preview_returns_401_without_jwt(anon_client: TestClient) -> None:
    """Sans Authorization Bearer, le guard rejette."""
    response = anon_client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")
    # Backend NEXYA renvoie 401 AUTH_TOKEN_INVALID via le guard
    assert response.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════
# 8. Content-Length cohérent
# ══════════════════════════════════════════════════════════════


def test_preview_content_length_header_matches_pdf_size(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-Length doit matcher exactement la taille du PDF."""
    fake_pdf = b"%PDF-1.4\n" + b"x" * 5000 + b"\n%%EOF"
    monkeypatch.setattr(
        PreviewService,
        "get_cached_or_generate",
        AsyncMock(
            return_value=PreviewResult(
                pdf_bytes=fake_pdf,
                from_cache=False,
                truncated=False,
                size_bytes=len(fake_pdf),
            )
        ),
    )

    response = client.get(f"/files/{_FAKE_UPLOAD_ID}/preview")

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(fake_pdf))
    assert len(response.content) == len(fake_pdf)
