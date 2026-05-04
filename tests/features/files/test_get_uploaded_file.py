"""
Tests d'intégration — `GET /files/{upload_id}` (Session D2.5, 2026-05-04).

Cible la nouvelle route ajoutée par D2.5 que le client Flutter (D2 front)
consomme pour poller l'état d'indexation RAG via la sentinelle
`chunks_indexed_at` après upload d'un PDF/DOCX/TXT/MD.

Couvre :
- 200 happy path : enveloppe NexyaResponse + presigned URL fraîche +
  champ `chunks_indexed_at` exposé (None tant que le worker D4 n'a pas
  posé la sentinelle, datetime ISO une fois indexé).
- 404 IDOR-safe : `FileUploadService.get_for_user` lève
  `ResourceNotFoundException` si l'upload n'appartient pas à l'user
  (ou est soft-deleted, ou n'existe pas) — le router propage tel quel,
  jamais 403 (anti-énumération UUID).
- 401 sans JWT : sans `Authorization: Bearer ...`, le guard
  `get_current_user` rejette avant toute logique métier.
- Presigned URL régénérée à chaque appel : `presigned_url_for` est
  appelé une fois par GET (TTL 30 min côté serveur), le client peut
  refetch pour rafraîchir une URL expirée sans re-uploader.
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
from app.features.auth.models import User
from app.features.files.models import UploadedFile
from app.features.files.service import FileUploadService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_UPLOAD_ID = uuid.UUID("11111111-0000-4000-8000-000000000001")
_FAKE_URL_1 = "mock://nexya-media/uploads/abcd.pdf?expires=111&sig=aaa"
_FAKE_URL_2 = "mock://nexya-media/uploads/abcd.pdf?expires=222&sig=bbb"


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_upload(
    *,
    upload_id: uuid.UUID | None = None,
    chunks_indexed_at: datetime | None = None,
) -> UploadedFile:
    now = datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)
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
    row.id = upload_id or _FAKE_UPLOAD_ID
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
    # D2.5 — sentinelle RAG.
    row.chunks_indexed_at = chunks_indexed_at
    return row


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
    """Client SANS override d'auth — utilisé pour vérifier le 401."""
    with TestClient(app) as c:
        yield c


# ══════════════════════════════════════════════════════════════
# 1. Happy path — 200 + envelope + presigned URL + chunks_indexed_at
# ══════════════════════════════════════════════════════════════


def test_get_uploaded_file_returns_200_with_envelope_and_presigned_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sans `chunks_indexed_at` posé : la sentinelle est None (cas
    immédiatement post-upload, le worker D4 n'a pas encore tourné)."""
    row = _make_fake_upload(chunks_indexed_at=None)
    monkeypatch.setattr(FileUploadService, "get_for_user", AsyncMock(return_value=row))
    monkeypatch.setattr(
        FileUploadService,
        "presigned_url_for",
        AsyncMock(return_value=_FAKE_URL_1),
    )

    response = client.get(f"/files/{row.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(row.id)
    assert body["data"]["url"] == _FAKE_URL_1
    assert body["data"]["mime_type"] == "application/pdf"
    assert body["data"]["virus_scan_status"] == "clean"
    assert body["data"]["extraction_status"] == "ok"
    # Sentinelle RAG explicitement None (pas absente du payload).
    assert "chunks_indexed_at" in body["data"]
    assert body["data"]["chunks_indexed_at"] is None


def test_get_uploaded_file_exposes_chunks_indexed_at_when_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une fois le worker D4 passé, `chunks_indexed_at` est un datetime ISO
    — le client Flutter détecte le flip et bascule son badge UI
    « Indexation… » → « Prêt RAG »."""
    indexed_at = datetime(2026, 5, 4, 10, 30, 45, tzinfo=UTC)
    row = _make_fake_upload(chunks_indexed_at=indexed_at)
    monkeypatch.setattr(FileUploadService, "get_for_user", AsyncMock(return_value=row))
    monkeypatch.setattr(FileUploadService, "presigned_url_for", AsyncMock(return_value=_FAKE_URL_1))

    response = client.get(f"/files/{row.id}")

    assert response.status_code == 200
    body = response.json()
    chunks_indexed_at = body["data"]["chunks_indexed_at"]
    assert chunks_indexed_at is not None
    # Format ISO 8601 sérialisé par Pydantic — contient la date + l'heure.
    assert chunks_indexed_at.startswith("2026-05-04T10:30:45")


# ══════════════════════════════════════════════════════════════
# 2. 404 IDOR-safe — upload absent / pas à l'user / soft-deleted
# ══════════════════════════════════════════════════════════════


def test_get_uploaded_file_returns_404_when_not_owned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`FileUploadService.get_for_user` raise pour TROIS cas (UUID inconnu,
    upload d'un autre user, upload soft-deleted) — le router propage le
    404 sans distinguer (anti-énumération UUID, jamais 403)."""
    monkeypatch.setattr(
        FileUploadService,
        "get_for_user",
        AsyncMock(side_effect=ResourceNotFoundException("Upload")),
    )

    response = client.get(f"/files/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_get_uploaded_file_returns_422_on_malformed_uuid(client: TestClient) -> None:
    """Un path qui n'est pas un UUID valide → 422 Pydantic AVANT toute
    logique métier (FastAPI parse l'argument typé `uuid.UUID` à l'entrée)."""
    response = client.get("/files/not-a-uuid")
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 3. 401 — pas de JWT
# ══════════════════════════════════════════════════════════════


def test_get_uploaded_file_returns_401_without_jwt(anon_client: TestClient) -> None:
    """Sans `Authorization: Bearer ...`, le guard `get_current_user`
    rejette avant toute logique métier."""
    response = anon_client.get(f"/files/{_FAKE_UPLOAD_ID}")
    # FastAPI/Starlette renvoient 401 ou 403 selon le HTTPBearer config —
    # NEXYA utilise un guard custom qui mappe sur 401 AUTH_TOKEN_INVALID
    # (cf. `app/core/auth/guards.py`).
    assert response.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════
# 4. Presigned URL régénérée à chaque appel
# ══════════════════════════════════════════════════════════════


def test_get_uploaded_file_regenerates_presigned_url_on_each_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le client peut re-fetcher cet endpoint pour rafraîchir une URL
    expirée sans re-uploader — `presigned_url_for` est appelé à chaque
    GET, garantissant un TTL 30 min repartant de zéro."""
    row = _make_fake_upload(chunks_indexed_at=None)
    monkeypatch.setattr(FileUploadService, "get_for_user", AsyncMock(return_value=row))

    # Mock qui retourne 2 URLs distinctes pour les 2 appels successifs.
    presigned_mock = AsyncMock(side_effect=[_FAKE_URL_1, _FAKE_URL_2])
    monkeypatch.setattr(FileUploadService, "presigned_url_for", presigned_mock)

    r1 = client.get(f"/files/{row.id}")
    r2 = client.get(f"/files/{row.id}")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["data"]["url"] == _FAKE_URL_1
    assert r2.json()["data"]["url"] == _FAKE_URL_2
    # Un appel à presigned_url_for par GET — pas de cache silencieux.
    assert presigned_mock.await_count == 2
