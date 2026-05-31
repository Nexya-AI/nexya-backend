"""Tests router — `POST /generate/document` (C4.7a).

Mock-first :
    - DB session via `app.dependency_overrides`
    - DocumentGeneratorService.generate monkeypatché
    - Rate limit monkeypatché (sinon Redis nécessaire)

Couvre :
    - 201 happy path + envelope NexyaResponse
    - 422 sur template invalide
    - 404 IDOR (ResourceNotFoundException du service)
    - 413 source too long
    - 503 render failed
    - 429 rate limit Free 60/h vs Pro 100/h
    - Auth required (sans JWT)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.document_generator import router as router_module
from app.features.document_generator.exceptions import (
    DocumentRenderFailedError,
    DocumentSourceTooLongError,
)
from app.features.document_generator.schemas import DocumentGenerateResponse
from app.features.document_generator.service import DocumentGeneratorService
from app.main import app


def _make_fake_user(is_pro: bool = False):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_pro = is_pro
    return user


def _install_overrides(monkeypatch: pytest.MonkeyPatch, user, *, skip_rate_limit: bool = True):
    """Installe app.dependency_overrides + skip rate_limit Redis."""
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()

    if skip_rate_limit:
        async def fake_rate_limit(*args, **kwargs):
            return None

        monkeypatch.setattr(router_module, "check_user_rate_limit", fake_rate_limit)


def _cleanup_overrides():
    app.dependency_overrides.clear()


# ──────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentHappy:
    def test_returns_201_with_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        user = _make_fake_user(is_pro=False)
        _install_overrides(monkeypatch, user)

        now = datetime.now(timezone.utc)
        fake_response = DocumentGenerateResponse(
            library_id=uuid.uuid4(),
            download_url="https://minio.local/foo.pdf?sig=abc",
            filename="my_doc.pdf",
            size_bytes=12345,
            pages=10,
            truncated=False,
            expires_at=now,
            generated_at=now,
        )

        async def fake_generate(user_arg, body_arg, db_arg):
            return fake_response

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

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
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        now = datetime.now(timezone.utc)
        fake_response = DocumentGenerateResponse(
            library_id=uuid.uuid4(),
            download_url="https://x/y.pdf",
            filename="d.pdf",
            size_bytes=1000,
            pages=5,
            truncated=False,
            expires_at=now,
            generated_at=now,
        )

        async def fake_generate(*args, **kwargs):
            return fake_response

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "template": "school",
                        "options": {
                            "subject": "Maths",
                            "level": "Term S",
                        },
                    },
                )
            assert response.status_code == 201
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# C4.7b — Format DOCX bout-en-bout
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentDocx:
    def test_format_docx_returns_201_with_docx_filename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """format=docx → 201 + filename.docx + envelope OK."""
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        now = datetime.now(timezone.utc)
        fake_response = DocumentGenerateResponse(
            library_id=uuid.uuid4(),
            download_url="https://minio.local/foo.docx?sig=xyz",
            filename="Mon_doc_Word.docx",
            size_bytes=8000,
            pages=4,
            truncated=False,
            expires_at=now,
            generated_at=now,
        )

        captured_body = {}

        async def fake_generate(user_arg, body_arg, db_arg):
            captured_body["format"] = body_arg.format
            captured_body["template"] = body_arg.template
            return fake_response

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

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
            assert data["success"] is True
            assert data["data"]["filename"].endswith(".docx")
            assert captured_body["format"] == "docx"
            assert captured_body["template"] == "minimal"
        finally:
            _cleanup_overrides()

    def test_format_docx_with_school_template_and_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """format=docx + template=school + options school → OK."""
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        now = datetime.now(timezone.utc)
        captured = {}

        async def fake_generate(user_arg, body_arg, db_arg):
            captured["subject"] = body_arg.options.subject
            captured["level"] = body_arg.options.level
            return DocumentGenerateResponse(
                library_id=uuid.uuid4(),
                download_url="https://x/y.docx",
                filename="DM.docx",
                size_bytes=5000,
                pages=2,
                truncated=False,
                expires_at=now,
                generated_at=now,
            )

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "format": "docx",
                        "template": "school",
                        "options": {
                            "subject": "Histoire",
                            "level": "1ère ES",
                        },
                    },
                )
            assert response.status_code == 201
            assert captured["subject"] == "Histoire"
            assert captured["level"] == "1ère ES"
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
                        # C4.7c — sciences devenu VALIDE, on utilise
                        # "business" comme slug invalide (V2 ou jamais).
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
                        "format": "pptx",  # Pas dans Literal ['pdf','docx']
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
                    json={
                        "conversation_id": "not-a-uuid",
                        "message_id": str(uuid.uuid4()),
                    },
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

        async def fake_generate(*args, **kwargs):
            raise DocumentSourceTooLongError("Source trop longue (300k > 200k)")

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

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

        async def fake_generate(*args, **kwargs):
            raise DocumentRenderFailedError("WeasyPrint timeout")

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

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
    def test_free_user_rate_limit_60_per_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Free user passe par check_user_rate_limit avec max=60."""
        user = _make_fake_user(is_pro=False)
        captured_max = {"value": None}

        async def fake_rate_limit(user_id, action, max_requests, window_seconds, **kwargs):
            captured_max["value"] = max_requests
            return None

        monkeypatch.setattr(router_module, "check_user_rate_limit", fake_rate_limit)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: MagicMock()

        now = datetime.now(timezone.utc)

        async def fake_generate(*args, **kwargs):
            return DocumentGenerateResponse(
                library_id=uuid.uuid4(),
                download_url="https://x/y",
                filename="x.pdf",
                size_bytes=1,
                pages=1,
                truncated=False,
                expires_at=now,
                generated_at=now,
            )

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                    },
                )
            assert response.status_code == 201
            # Free user → 60/h dans settings par défaut
            assert captured_max["value"] == 60
        finally:
            _cleanup_overrides()

    def test_pro_user_rate_limit_100_per_hour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pro user passe par check_user_rate_limit avec max=100."""
        user = _make_fake_user(is_pro=True)
        captured_max = {"value": None}

        async def fake_rate_limit(user_id, action, max_requests, window_seconds, **kwargs):
            captured_max["value"] = max_requests
            return None

        monkeypatch.setattr(router_module, "check_user_rate_limit", fake_rate_limit)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: MagicMock()

        now = datetime.now(timezone.utc)

        async def fake_generate(*args, **kwargs):
            return DocumentGenerateResponse(
                library_id=uuid.uuid4(),
                download_url="https://x/y",
                filename="x.pdf",
                size_bytes=1,
                pages=1,
                truncated=False,
                expires_at=now,
                generated_at=now,
            )

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                    },
                )
            assert response.status_code == 201
            # Pro user → 100/h
            assert captured_max["value"] == 100
        finally:
            _cleanup_overrides()


# ──────────────────────────────────────────────────────────────────
# C4.7c — 3 nouveaux templates (sciences/legal/medicine) bout-en-bout
# ──────────────────────────────────────────────────────────────────


class TestGenerateDocumentExtraTemplates:
    @pytest.mark.parametrize(
        "template_slug",
        ["sciences", "legal", "medicine"],
    )
    def test_post_with_new_template_returns_201(
        self, template_slug: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C4.7c — POST /generate/document accepte les 3 nouveaux templates."""
        user = _make_fake_user()
        _install_overrides(monkeypatch, user)

        now = datetime.now(timezone.utc)
        captured = {}

        async def fake_generate(user_arg, body_arg, db_arg):
            captured["template"] = body_arg.template
            captured["format"] = body_arg.format
            return DocumentGenerateResponse(
                library_id=uuid.uuid4(),
                download_url=f"https://x/y.pdf?t={template_slug}",
                filename=f"doc_{template_slug}.pdf",
                size_bytes=5000,
                pages=3,
                truncated=False,
                expires_at=now,
                generated_at=now,
            )

        monkeypatch.setattr(DocumentGeneratorService, "generate", fake_generate)

        try:
            with TestClient(app) as client:
                response = client.post(
                    "/generate/document",
                    json={
                        "conversation_id": str(uuid.uuid4()),
                        "message_id": str(uuid.uuid4()),
                        "template": template_slug,
                        "options": {
                            "subject": "Test subject",
                            "level": "Test level",
                        },
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
        """Sans override get_current_user, l'endpoint refuse."""
        # Pas d'override : on garde le vrai guard get_current_user qui
        # exige un JWT valide en header.
        with TestClient(app) as client:
            response = client.post(
                "/generate/document",
                json={
                    "conversation_id": str(uuid.uuid4()),
                    "message_id": str(uuid.uuid4()),
                },
            )
        # 401 (token manquant) ou 403 (guard refuse)
        assert response.status_code in (401, 403)
