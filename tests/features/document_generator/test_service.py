"""Tests unitaires — DocumentGeneratorService (C4.7a).

Mock-first strict :
    - DB session fake (pas de Postgres réel)
    - WeasyPrint mocké (pas de cairo/pango runtime)
    - LibraryService mocké (pas de MinIO réel)

Couvre :
    - Owner-check IDOR-safe (JOIN messages × conversations)
    - 404 si message inexistant / soft-deleted
    - 413 si content > cap
    - Helper _sanitize_filename robustesse
    - Happy path complet (message → PDF → Library → response)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.document_generator import service as service_module
from app.features.document_generator import weasyprint_renderer as renderer_module
from app.features.document_generator.exceptions import DocumentSourceTooLongError
from app.features.document_generator.schemas import (
    DocumentGenerateOptions,
    DocumentGenerateRequest,
)
from app.features.document_generator.service import (
    DocumentGeneratorService,
    _sanitize_filename,
)


# ──────────────────────────────────────────────────────────────────
# _sanitize_filename
# ──────────────────────────────────────────────────────────────────


class TestSanitizeFilename:
    def test_clean_filename_preserved(self) -> None:
        assert _sanitize_filename("hello_world-123", "fallback") == "hello_world-123"

    def test_spaces_replaced_with_underscores(self) -> None:
        assert _sanitize_filename("Hello World", "fallback") == "Hello_World"

    def test_special_chars_replaced(self) -> None:
        assert _sanitize_filename("file/with:bad*chars", "fallback") == "file_with_bad_chars"

    def test_accented_chars_replaced(self) -> None:
        # Les accents sont hors `[a-zA-Z0-9_\-\.]+` donc replaced
        assert "é" not in _sanitize_filename("Devoir école", "fallback")

    def test_empty_returns_fallback(self) -> None:
        assert _sanitize_filename("", "default_name") == "default_name"

    def test_only_special_chars_returns_fallback(self) -> None:
        assert _sanitize_filename("///***", "fallback") == "fallback"

    def test_max_length_100(self) -> None:
        long_title = "a" * 200
        result = _sanitize_filename(long_title, "fallback")
        assert len(result) <= 100

    def test_leading_dots_stripped(self) -> None:
        assert not _sanitize_filename(".hidden_file", "fallback").startswith(".")


# ──────────────────────────────────────────────────────────────────
# _get_owned_message_content — IDOR safety
# ──────────────────────────────────────────────────────────────────


class TestGetOwnedMessageContent:
    @pytest.mark.asyncio
    async def test_returns_content_when_message_owned(self) -> None:
        """Happy path : message existe et appartient à l'user."""
        cid = uuid.uuid4()
        mid = uuid.uuid4()
        uid = uuid.uuid4()

        fake_message = MagicMock()
        fake_message.content = "# Hello\n\nContent markdown"

        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)

        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        content = await DocumentGeneratorService._get_owned_message_content(
            conversation_id=cid,
            message_id=mid,
            user_id=uid,
            db=fake_db,
        )
        assert content == "# Hello\n\nContent markdown"
        # JOIN exécuté en 1 SELECT
        assert fake_db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_raises_404_when_message_not_owned(self) -> None:
        """IDOR-safe : 404 si message d'un autre user (jamais 403)."""
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=None)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        with pytest.raises(ResourceNotFoundException):
            await DocumentGeneratorService._get_owned_message_content(
                conversation_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                db=fake_db,
            )

    @pytest.mark.asyncio
    async def test_raises_404_when_message_content_empty(self) -> None:
        """Message vide (placeholder/error/cancelled) → 404."""
        fake_message = MagicMock()
        fake_message.content = ""

        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        with pytest.raises(ResourceNotFoundException):
            await DocumentGeneratorService._get_owned_message_content(
                conversation_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                db=fake_db,
            )


# ──────────────────────────────────────────────────────────────────
# generate — orchestration complète
# ──────────────────────────────────────────────────────────────────


def _make_fake_user(is_pro: bool = False):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_pro = is_pro
    return user


def _make_fake_message(content: str = "# Titre\n\nUn paragraphe.\n"):
    msg = MagicMock()
    msg.content = content
    return msg


def _install_fake_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pdf_bytes: bytes = b"%PDF-1.4 fake",
    pages: int = 3,
    truncated: bool = False,
) -> dict:
    """Installe les mocks WeasyPrint + LibraryService.

    Retourne un dict capture qui contient les arguments capturés
    (pour assertions dans les tests).
    """
    captures: dict = {"library_calls": [], "render_calls": []}

    # Mock WeasyPrint render
    def fake_render_sync(html_content: str) -> bytes:
        captures["render_calls"].append(html_content)
        return pdf_bytes

    def fake_post_process(pdf_bytes_in: bytes, *, max_pages: int):
        return renderer_module.RenderedPdf(
            pdf_bytes=pdf_bytes_in,
            pages=pages,
            truncated=truncated,
            size_bytes=len(pdf_bytes_in),
        )

    monkeypatch.setattr(renderer_module, "_render_pdf_sync", fake_render_sync)
    monkeypatch.setattr(renderer_module, "_post_process_pdf_sync", fake_post_process)

    # Mock LibraryService
    fake_library_item = MagicMock()
    fake_library_item.id = uuid.uuid4()

    async def fake_create_from_bytes(*args, **kwargs):
        captures["library_calls"].append(kwargs)
        return fake_library_item

    async def fake_presigned_url_for(item, *, ttl_seconds=None):
        captures["presigned_ttl"] = ttl_seconds
        return f"https://minio.local/{item.id}?sig=abc"

    from app.features.library.service import LibraryService

    monkeypatch.setattr(LibraryService, "create_from_bytes", fake_create_from_bytes)
    monkeypatch.setattr(LibraryService, "presigned_url_for", fake_presigned_url_for)

    return captures


class TestGenerateHappyPath:
    @pytest.mark.asyncio
    async def test_full_pipeline_minimal_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path complet : message → PDF → Library → response."""
        # Mock DB
        fake_message = _make_fake_message("# Test\n\nContenu de test.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        # Mock pipeline
        captures = _install_fake_pipeline(monkeypatch, pages=5)

        user = _make_fake_user(is_pro=False)
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            template="minimal",
            title="Mon document",
        )

        result = await DocumentGeneratorService.generate(user, body, fake_db)

        assert result.pages == 5
        assert result.truncated is False
        assert result.filename == "Mon_document.pdf"
        assert result.download_url.startswith("https://minio.local/")
        assert result.size_bytes > 0
        # LibraryService.create_from_bytes appelé avec bons paramètres
        assert len(captures["library_calls"]) == 1
        call = captures["library_calls"][0]
        assert call["type_"] == "document"
        assert call["file_type"] == "pdf"
        assert call["source"] == "generated"
        assert call["mime_type"] == "application/pdf"
        assert call["metadata_json"]["template"] == "minimal"
        assert call["metadata_json"]["pages"] == 5

    @pytest.mark.asyncio
    async def test_school_template_passes_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Template school : options subject/level apparaissent dans HTML."""
        fake_message = _make_fake_message("Devoir maths")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_pipeline(monkeypatch)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            template="school",
            options=DocumentGenerateOptions(
                subject="Maths",
                level="Terminale",
                date_iso="2026-05-30",
            ),
            title="DM",
        )

        await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)

        # HTML render appelé avec les options school
        rendered_html = captures["render_calls"][0]
        assert "Maths" in rendered_html
        assert "Terminale" in rendered_html
        assert "2026-05-30" in rendered_html

    @pytest.mark.asyncio
    async def test_truncated_pdf_flag_propagated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_message = _make_fake_message("Long content")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        _install_fake_pipeline(monkeypatch, pages=50, truncated=True)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            template="minimal",
            title="Long doc",
        )

        result = await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)
        assert result.truncated is True
        assert result.pages == 50

    @pytest.mark.asyncio
    async def test_default_title_when_none_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_message = _make_fake_message("X")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        _install_fake_pipeline(monkeypatch)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            template="minimal",
            title=None,
        )

        result = await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)
        # Filename non vide, basé sur "document_YYYY-MM-DD"
        assert result.filename.endswith(".pdf")
        assert "document" in result.filename


class TestGenerateErrors:
    @pytest.mark.asyncio
    async def test_source_too_long_raises_413(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Content > cap chars → DocumentSourceTooLongError."""
        # Cap settings à une petite valeur pour le test
        from app.config import settings

        monkeypatch.setattr(settings, "documents_generator_max_source_chars", 100)

        fake_message = _make_fake_message("a" * 200)
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
        )

        with pytest.raises(DocumentSourceTooLongError) as exc_info:
            await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)
        assert exc_info.value.code == "DOCUMENT_SOURCE_TOO_LONG"

    @pytest.mark.asyncio
    async def test_message_not_owned_raises_404(self) -> None:
        """Message pas owned par user → ResourceNotFoundException."""
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=None)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
        )

        with pytest.raises(ResourceNotFoundException):
            await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)
