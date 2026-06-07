"""Tests router — `POST /generate/document` + `GET .../jobs/{id}` (C4.7a + C4.12).

Mock-first :
    - DB session via `app.dependency_overrides`
    - DocumentGeneratorService.generate_or_enqueue monkeypatché (le router
      délègue à l'orchestrateur sync/async depuis C4.12)
    - Rate limit monkeypatché (sinon Redis nécessaire)

Couvre :
    - 201 happy path sync + envelope NexyaResponse (C4.7a)
    - 202 async (DocumentAsyncResult → job enqueué — C4.12)
    - GET /jobs/{id} polling 200 + 404 IDOR (C4.12)
    - 422 template/format/uuid/title invalides
    - 413 source too long / 503 render failed
    - 429 rate limit Free 60/h vs Pro 100/h
    - Auth required (sans JWT)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import ResourceNotFoundException
from app.features.document_generator import router as router_module
from app.features.document_generator.exceptions import (
    DocumentRenderFailedError,
    DocumentSourceTooLongError,
)
from app.features.document_generator.job_service import DocumentJobService
from app.features.document_generator.schemas import (
    DocumentGenerateResponse,
    DocumentJobResponse,
)
from app.features.document_generator.service import (
    DocumentAsyncResult,
    DocumentGeneratorService,
    DocumentSyncResult,
)
from app.main import app


def _make_fake_user(is_pro: bool = False):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_pro = is_pro
    return user


def _install_overrides(monkeypatch: pytest.MonkeyPatch, user, *, skip_rate_limit: bool = True):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    if skip_rate_limit:
        async def fake_rate_limit(*args, **kwargs):
            return None

        monkeypatch.setattr(router_module, "check_user_rate_limit", fake_rate_limit)


def _cleanup_overrides():
    app.dependency_overrides.clear()


def _fake_response(**overrides) -> DocumentGenerateResponse:
    now = datetime.now(UTC)
    base = dict(
        library_id=uuid.uuid4(),
        download_url="https://minio.local/foo.pdf?sig=abc",
        filename="my_doc.pdf",
        size_bytes=12345,
        pages=10,
        truncated=False,
        expires_at=now,
        generated_at=now,
    )
    base.update(overrides)
    return DocumentGenerateResponse(**base)


def _patch_sync(monkeypatch: pytest.MonkeyPatch, response: DocumentGenerateResponse, *, capture=None):
    """Monkeypatch generate_or_enqueue → DocumentSyncResult (chemin 201)."""

    async def fake_orchestrate(user_arg, body_arg, db_arg):
        if capture is not None:
            capture["format"] = body_arg.format
            capture["template"] = body_arg.template
            capture["subject"] = body_arg.options.subject
            capture["level"] = body_arg.options.level
        return DocumentSyncResult(response=response)

    monkeypatch.setattr(
        DocumentGeneratorService, "generate_or_enqueue", fake_orchestrate
    )


def _patch_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception):
    async def fake_orchestrate(*args, **kwargs):
        raise exc

    monkeypatch.setattr(
        DocumentGeneratorService, "generate_or_enqueue", fake_orchestrate
    )


# ──────────────────────────────────────────────────────────────────
# Happy path SYNC (201)
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentHappy:
    def test_returns_201_with_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _make_fake_user(is_pro=False)
        _install_overrides(monkeypatch, user)
        _patch_sync(monkeypatch, _fake_response(filename="my_doc.pdf", pages=10))
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "template": "minimal",
                        "title": "Mon document",
                    },
                )
            assert response.status_code == 201
            data = response.json()
            assert data["success"] is True
            assert data["data"]["filename"] == "my_doc.pdf"
            assert data["data"]["pages"] == 10
            assert data["data"]["truncated"] is False
        finally:
            _cleanup_overrides()

    def test_school_template_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        _patch_sync(monkeypatch, _fake_response(filename="d.pdf", pages=5))
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "template": "school",
                        "options": {"subject": "Maths", "level": "Term S"},
                    },
                )
            assert response.status_code == 201
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# C4.7b — Format DOCX
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentDocx:
    def test_format_docx_returns_201_with_docx_filename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        captured = {}
        _patch_sync(
            monkeypatch,
            _fake_response(filename="Mon_doc_Word.docx", pages=4),
            capture=captured,
        )
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "format": "docx",
                        "template": "minimal",
                        "title": "Mon doc Word",
                    },
                )
            assert response.status_code == 201
            data = response.json()
            assert data["data"]["filename"].endswith(".docx")
            assert captured["format"] == "docx"
            assert captured["template"] == "minimal"
        finally:
            _cleanup_overrides()

    def test_format_docx_with_school_template_and_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        captured = {}
        _patch_sync(
            monkeypatch, _fake_response(filename="DM.docx", pages=2), capture=captured
        )
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "format": "docx",
                        "template": "school",
                        "options": {"subject": "Histoire", "level": "1ère ES"},
                    },
                )
            assert response.status_code == 201
            assert captured["subject"] == "Histoire"
            assert captured["level"] == "1ère ES"
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# C4.12 — Chemin asynchrone (202) + polling
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentAsync:
    def test_returns_202_when_async(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Doc lourd → generate_or_enqueue renvoie DocumentAsyncResult → 202."""
        _install_overrides(monkeypatch, _make_fake_user())

        conv_id = uuid.uuid4()
        msg_id = uuid.uuid4()
        fake_job = MagicMock()
        fake_job.id = uuid.uuid4()
        fake_job.conversation_id = conv_id
        fake_job.message_id = msg_id
        fake_job.format = "pdf"

        async def fake_orchestrate(*args, **kwargs):
            return DocumentAsyncResult(job=fake_job)

        monkeypatch.setattr(
            DocumentGeneratorService, "generate_or_enqueue", fake_orchestrate
        )

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(conv_id),
                        "message_id": str(msg_id),
                    },
                )
            assert response.status_code == 202
            data = response.json()
            assert data["success"] is True
            assert data["data"]["status"] == "processing"
            assert data["data"]["job_id"] == str(fake_job.id)
            assert data["data"]["conversation_id"] == str(conv_id)
            assert data["data"]["format"] == "pdf"
        finally:
            _cleanup_overrides()

    def test_get_job_returns_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /jobs/{id} → 200 + DocumentJobResponse."""
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        job_id = uuid.uuid4()
        now = datetime.now(UTC)
        job_response = DocumentJobResponse(
            job_id=job_id,
            status="done",
            format="pdf",
            template="minimal",
            library_id=uuid.uuid4(),
            download_url="https://minio.local/x.pdf?sig=fresh",
            filename="x.pdf",
            pages=12,
            size_bytes=9999,
            truncated=False,
            error_code=None,
            created_at=now,
            completed_at=now,
        )

        async def fake_get_owned(jid, u, db):
            return MagicMock()

        async def fake_to_response(job, u, db):
            return job_response

        monkeypatch.setattr(DocumentJobService, "get_owned_job", fake_get_owned)
        monkeypatch.setattr(DocumentJobService, "to_response", fake_to_response)

        try:
            with TestClient(app) as client:
                response = client.get(f"/generate/document/jobs/{job_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["data"]["status"] == "done"
            assert data["data"]["download_url"].endswith("sig=fresh")
            assert data["data"]["pages"] == 12
        finally:
            _cleanup_overrides()

    def test_get_job_404_idor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /jobs/{id} d'un job non possédé → 404 IDOR-safe."""
        _install_overrides(monkeypatch, _make_fake_user())

        async def fake_get_owned(jid, u, db):
            raise ResourceNotFoundException("Job de génération")

        monkeypatch.setattr(DocumentJobService, "get_owned_job", fake_get_owned)

        try:
            with TestClient(app) as client:
                response = client.get(f"/generate/document/jobs/{uuid.uuid4()}")
            assert response.status_code == 404
            assert response.json()["success"] is False
        finally:
            _cleanup_overrides()

    def test_get_job_422_invalid_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        try:
            with TestClient(app) as client:
                response = client.get("/generate/document/jobs/not-a-uuid")
            assert response.status_code == 422
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# Validation Pydantic
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentValidation:
    def test_422_on_unknown_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "template": "business",
                    },
                )
            assert response.status_code == 422
        finally:
            _cleanup_overrides()

    def test_422_on_unknown_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "format": "pptx",
                    },
                )
            assert response.status_code == 422
        finally:
            _cleanup_overrides()

    def test_422_on_invalid_uuid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={"conversation_id": "not-a-uuid", "message_id": str(uuid.uuid4())},
                )
            assert response.status_code == 422
        finally:
            _cleanup_overrides()

    def test_422_on_title_too_long(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "title": "x" * 201,
                    },
                )
            assert response.status_code == 422
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# Erreurs typées propagées
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentErrors:
    def test_413_on_source_too_long(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        _patch_raises(monkeypatch, DocumentSourceTooLongError("Source trop longue"))
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                    },
                )
            assert response.status_code == 413
            data = response.json()
            assert data["success"] is False
            assert data["code"] == "DOCUMENT_SOURCE_TOO_LONG"
        finally:
            _cleanup_overrides()

    def test_503_on_render_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        _patch_raises(monkeypatch, DocumentRenderFailedError("WeasyPrint timeout"))
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                    },
                )
            assert response.status_code == 503
            data = response.json()
            assert data["success"] is False
            assert data["code"] == "DOCUMENT_RENDER_FAILED"
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# Rate limit asymétrique Free vs Pro
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentRateLimit:
    def _run_with_captured_max(self, monkeypatch, user):
        captured_max = {"value": None}

        async def fake_rate_limit(user_id, action, max_requests, window_seconds, **kwargs):
            captured_max["value"] = max_requests
            return None

        monkeypatch.setattr(router_module, "check_user_rate_limit", fake_rate_limit)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: MagicMock()
        _patch_sync(monkeypatch, _fake_response(filename="x.pdf", pages=1))

        with TestClient(app) as client:
            response = client.post(
                "/generate/document",
                json={
                    "conversation_id": str(uuid.uuid4()),
                    "message_id": str(uuid.uuid4()),
                },
            )
        return response, captured_max["value"]

    def test_free_user_rate_limit_60_per_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        try:
            response, captured = self._run_with_captured_max(
                monkeypatch, _make_fake_user(is_pro=False)
            )
            assert response.status_code == 201
            assert captured == 60
        finally:
            _cleanup_overrides()

    def test_pro_user_rate_limit_100_per_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        try:
            response, captured = self._run_with_captured_max(
                monkeypatch, _make_fake_user(is_pro=True)
            )
            assert response.status_code == 201
            assert captured == 100
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# C4.7c — 3 nouveaux templates bout-en-bout
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentExtraTemplates:
    @pytest.mark.parametrize("template_slug", ["sciences", "legal", "medicine"])
    def test_post_with_new_template_returns_201(
        self, template_slug: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_overrides(monkeypatch, _make_fake_user())
        captured = {}
        _patch_sync(
            monkeypatch,
            _fake_response(filename=f"doc_{template_slug}.pdf", pages=3),
            capture=captured,
        )
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "template": template_slug,
                        "options": {"subject": "Test subject", "level": "Test level"},
                    },
                )
            assert response.status_code == 201
            assert captured["template"] == template_slug
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# Auth required
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentAuth:
    def test_401_or_403_without_auth(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/generate/document",
                json={
                    "conversation_id": str(uuid.uuid4()),
                    "message_id": str(uuid.uuid4()),
                },
            )
        assert response.status_code in (401, 403)
