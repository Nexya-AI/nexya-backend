"""Tests unitaires — DocumentGeneratorService (C4.7a + C4.7b).

Mock-first strict :
    - DB session fake (pas de Postgres réel)
    - WeasyPrint mocké (pas de cairo/pango runtime)
    - python-docx mocké (pas de Word natif runtime)
    - LibraryService mocké (pas de MinIO réel)

Couvre :
    - Owner-check IDOR-safe (JOIN messages × conversations)
    - 404 si message inexistant / soft-deleted
    - 413 si content > cap
    - Helper _sanitize_filename robustesse
    - Happy path complet PDF (message → PDF → Library → response)
    - Happy path complet DOCX (message → DOCX → Library → response)
    - Dispatch format=pdf vs format=docx (mime_type, file_type, provider, version)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.document_generator import docx_renderer as docx_renderer_module
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


def _install_fake_docx_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    docx_bytes: bytes = b"PK\x03\x04fake-docx",
    pages: int = 3,
    truncated: bool = False,
) -> dict:
    """Installe les mocks python-docx + LibraryService pour C4.7b."""
    captures: dict = {"library_calls": [], "render_calls": []}

    async def fake_render_docx(**kwargs):  # type: ignore[no-untyped-def]
        captures["render_calls"].append(kwargs)
        return docx_renderer_module.RenderedDocx(
            docx_bytes=docx_bytes,
            pages=pages,
            truncated=truncated,
            size_bytes=len(docx_bytes),
        )

    monkeypatch.setattr(service_module, "render_markdown_to_docx", fake_render_docx)

    fake_library_item = MagicMock()
    fake_library_item.id = uuid.uuid4()

    async def fake_create_from_bytes(*args, **kwargs):
        captures["library_calls"].append(kwargs)
        return fake_library_item

    async def fake_presigned_url_for(item, *, ttl_seconds=None):
        captures["presigned_ttl"] = ttl_seconds
        return f"https://minio.local/{item.id}?sig=docx"

    from app.features.library.service import LibraryService

    monkeypatch.setattr(LibraryService, "create_from_bytes", fake_create_from_bytes)
    monkeypatch.setattr(LibraryService, "presigned_url_for", fake_presigned_url_for)

    return captures


class TestGenerateDocxFormat:
    """C4.7b — Dispatch format=docx bout-en-bout."""

    @pytest.mark.asyncio
    async def test_docx_pipeline_minimal_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """format=docx déclenche docx_renderer + Library file_type=docx."""
        fake_message = _make_fake_message("# Doc DOCX\n\nContent.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_docx_pipeline(monkeypatch, pages=4)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="docx",
            template="minimal",
            title="Mon doc Word",
        )

        result = await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)

        # Render appelé via docx_renderer
        assert len(captures["render_calls"]) == 1
        render_kwargs = captures["render_calls"][0]
        assert render_kwargs["template_name"] == "minimal"
        assert render_kwargs["title"] == "Mon doc Word"

        # Library appelée avec file_type=docx + mime DOCX correct
        assert len(captures["library_calls"]) == 1
        lib_call = captures["library_calls"][0]
        assert lib_call["type_"] == "document"
        assert lib_call["file_type"] == "docx"
        assert (
            lib_call["mime_type"]
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert lib_call["provider"] == "python-docx"
        assert lib_call["metadata_json"]["format"] == "docx"
        # [C4.7d 2026-05-31] generator_version bumpé c47b-v1 → c47d-v1
        # (cohérent — watermark + C2PA enrichissent toute la metadata)
        assert lib_call["metadata_json"]["generator_version"] == "c47d-v1"

        # Response : filename .docx, pages, truncated
        assert result.filename.endswith(".docx")
        assert result.pages == 4
        assert result.truncated is False
        assert result.size_bytes == len(b"PK\x03\x04fake-docx")

    @pytest.mark.asyncio
    async def test_docx_school_template_forwards_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DOCX template school : options subject/level/date forwardées."""
        fake_message = _make_fake_message("Devoir maths.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_docx_pipeline(monkeypatch)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="docx",
            template="school",
            options=DocumentGenerateOptions(
                subject="Mathématiques",
                level="Terminale S",
                date_iso="2026-05-31",
            ),
            title="DM Word",
        )

        await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)

        render_kwargs = captures["render_calls"][0]
        assert render_kwargs["template_name"] == "school"
        opts = render_kwargs["options"]
        assert opts.subject == "Mathématiques"
        assert opts.level == "Terminale S"
        assert opts.date_iso == "2026-05-31"

    @pytest.mark.asyncio
    async def test_docx_truncated_flag_propagated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_message = _make_fake_message("Long content")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        _install_fake_docx_pipeline(monkeypatch, pages=50, truncated=True)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="docx",
            template="minimal",
            title="Long Word doc",
        )

        result = await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)
        assert result.truncated is True
        assert result.pages == 50

    @pytest.mark.asyncio
    async def test_pdf_default_format_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rétro-compat : sans format= explicite, défaut = pdf."""
        fake_message = _make_fake_message("Default pdf flow.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_pipeline(monkeypatch, pages=2)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            template="minimal",
            title="Default PDF",
            # format absent → défaut "pdf"
        )

        result = await DocumentGeneratorService.generate(_make_fake_user(), body, fake_db)

        assert result.filename.endswith(".pdf")
        lib_call = captures["library_calls"][0]
        assert lib_call["file_type"] == "pdf"
        assert lib_call["mime_type"] == "application/pdf"
        assert lib_call["provider"] == "weasyprint"
        assert lib_call["metadata_json"]["format"] == "pdf"


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


# ──────────────────────────────────────────────────────────────────
# C4.7d — Watermark + C2PA pipeline (Gate Pro + métadonnées)
# ──────────────────────────────────────────────────────────────────


class TestC47dWatermarkC2PAPipeline:
    """C4.7d — Gate Pro 403, métadonnées Library enrichies, C2PA PDF."""

    @pytest.mark.asyncio
    async def test_remove_watermark_free_user_raises_403_plan_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C4.7d — Free user qui tente remove_watermark=True → 403
        PLAN_REQUIRED AVANT render (économie WeasyPrint+pikepdf)."""
        from app.core.errors.exceptions import PlanRequiredException

        # Pas besoin de mock DB ni renderer — l'exception lève AVANT
        # le 1er SELECT message_content (gate Pro pre-flight).
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            remove_watermark=True,
        )

        with pytest.raises(PlanRequiredException) as exc_info:
            await DocumentGeneratorService.generate(
                _make_fake_user(is_pro=False),
                body,
                MagicMock(),  # db jamais consommée
            )
        # PlanRequiredException porte le code PLAN_REQUIRED + feature
        assert exc_info.value.code == "PLAN_REQUIRED"

    @pytest.mark.asyncio
    async def test_remove_watermark_pro_user_no_watermark_applied_metadata_traced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C4.7d — Pro + remove_watermark=true → watermark_applied=False
        + metadata.no_watermark_was_requested=True (pour wallet V2)."""
        fake_message = _make_fake_message("# Test\n\nContenu.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_pipeline(monkeypatch)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            remove_watermark=True,
        )
        result = await DocumentGeneratorService.generate(
            _make_fake_user(is_pro=True), body, fake_db
        )

        assert result.watermark_applied is False
        assert result.watermark_version is None
        # Metadata Library trace l'intent user pour facturation V2
        lib_meta = captures["library_calls"][0]["metadata_json"]
        assert lib_meta["has_watermark"] is False
        assert lib_meta["no_watermark_was_requested"] is True

    @pytest.mark.asyncio
    async def test_watermark_default_pro_user_applied_plus_c2pa_pdf(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C4.7d — Pro + remove_watermark=False (défaut) + format=pdf →
        watermark_applied=True + c2pa_applied=True (MockManifestProvider)."""
        fake_message = _make_fake_message("# Test\n\nContenu.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_pipeline(monkeypatch)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="pdf",
            template="minimal",
        )
        result = await DocumentGeneratorService.generate(
            _make_fake_user(is_pro=True), body, fake_db
        )

        # Watermark appliqué (asset PNG disponible, kill-switch ON par défaut)
        assert result.watermark_applied is True
        assert result.watermark_version is not None
        assert result.watermark_version.startswith("v")
        # C2PA appliqué via MockManifestProvider (factory mock-first auto
        # car clés X.509 vides par défaut en CI)
        assert result.c2pa_applied is True
        assert result.c2pa_manifest_id is not None
        assert result.c2pa_manifest_id.startswith("mock-c2pa-")
        assert result.c2pa_skip_reason is None
        # Metadata Library complète
        lib_meta = captures["library_calls"][0]["metadata_json"]
        assert lib_meta["has_watermark"] is True
        assert lib_meta["has_c2pa"] is True
        assert lib_meta["c2pa_manifest_id"] == result.c2pa_manifest_id

    @pytest.mark.asyncio
    async def test_watermark_default_docx_no_c2pa_v1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C4.7d — DOCX → watermark possiblement appliqué + c2pa_applied=False
        + c2pa_skip_reason='unsupported_format_docx' (V1, c2pa-rs ne
        supporte pas OOXML natif)."""
        fake_message = _make_fake_message("# Test\n\nContenu.")
        fake_result = MagicMock()
        fake_result.scalar_one_or_none = MagicMock(return_value=fake_message)
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=fake_result)

        captures = _install_fake_pipeline(monkeypatch)
        # Override DOCX renderer mock pour cohérence (le helper de base
        # est PDF-only). On force apply_watermark=True propagé.
        from app.features.document_generator import (
            docx_renderer as docx_module,
        )

        async def fake_docx(*args, **kwargs):
            return docx_module.RenderedDocx(
                docx_bytes=b"PK fake docx",
                pages=1,
                truncated=False,
                size_bytes=12,
                watermark_applied=kwargs.get("apply_watermark", False),
            )

        monkeypatch.setattr(docx_module, "render_markdown_to_docx", fake_docx)

        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="docx",
            template="minimal",
        )
        result = await DocumentGeneratorService.generate(
            _make_fake_user(is_pro=True), body, fake_db
        )

        # C2PA SKIP pour DOCX V1
        assert result.c2pa_applied is False
        assert result.c2pa_skip_reason == "unsupported_format_docx"
        assert result.c2pa_manifest_id is None
        # Metadata Library trace le skip explicite
        lib_meta = captures["library_calls"][0]["metadata_json"]
        assert lib_meta["has_c2pa"] is False
        assert lib_meta["c2pa_skip_reason"] == "unsupported_format_docx"
