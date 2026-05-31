"""Tests unitaires — schémas Pydantic document_generator (C4.7a + C4.7b).

Couvre :
    - Validation Literal `DocumentTemplate` (school/minimal accepté, autres rejetés)
    - Validation Literal `DocumentFormat` (pdf + docx accepté, autres rejetés)
    - Cap chars sur title (200) + options.subject/level (100)
    - Defaults DocumentGenerateOptions
    - Sérialisation/désérialisation round-trip
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.features.document_generator.schemas import (
    DocumentGenerateOptions,
    DocumentGenerateRequest,
    DocumentGenerateResponse,
)


# ──────────────────────────────────────────────────────────────────
# DocumentGenerateRequest
# ──────────────────────────────────────────────────────────────────


class TestDocumentGenerateRequestSchema:
    def test_minimal_request_accepts_required_fields_only(self) -> None:
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
        )
        assert body.format == "pdf"
        assert body.template == "minimal"
        assert body.title is None
        assert isinstance(body.options, DocumentGenerateOptions)

    def test_full_request_with_all_fields(self) -> None:
        cid = uuid.uuid4()
        mid = uuid.uuid4()
        body = DocumentGenerateRequest(
            conversation_id=cid,
            message_id=mid,
            format="pdf",
            template="school",
            options=DocumentGenerateOptions(
                subject="Mathématiques",
                level="Terminale S",
                date_iso="2026-05-30",
                page_numbers=True,
            ),
            title="Devoir sur les intégrales",
        )
        assert body.conversation_id == cid
        assert body.message_id == mid
        assert body.template == "school"
        assert body.options.subject == "Mathématiques"
        assert body.options.level == "Terminale S"
        assert body.title == "Devoir sur les intégrales"

    @pytest.mark.parametrize(
        "invalid_template",
        # C4.7c (2026-05-31) — sciences/legal/medicine devenus VALIDES,
        # retirés de cette liste. Nouveaux invalides : cooking/business/
        # unknown_xyz (V2 ou jamais).
        ["cooking", "business", "unknown_xyz", "medical", "../path", ""],
    )
    def test_rejects_unknown_template_via_literal(self, invalid_template: str) -> None:
        with pytest.raises(ValidationError):
            DocumentGenerateRequest(
                conversation_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                template=invalid_template,  # type: ignore[arg-type]
            )

    # ── C4.7c — Nouveaux templates sciences/legal/medicine ───────────
    @pytest.mark.parametrize(
        "valid_template",
        ["school", "minimal", "sciences", "legal", "medicine"],
    )
    def test_accepts_all_5_templates(self, valid_template: str) -> None:
        """C4.7c — Pydantic Literal accepte les 5 templates disponibles."""
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            template=valid_template,  # type: ignore[arg-type]
        )
        assert body.template == valid_template

    @pytest.mark.parametrize(
        "fmt,tmpl",
        [
            ("pdf", "sciences"),
            ("docx", "sciences"),
            ("pdf", "legal"),
            ("docx", "legal"),
            ("pdf", "medicine"),
            ("docx", "medicine"),
        ],
    )
    def test_accepts_6_new_template_format_combinations(
        self, fmt: str, tmpl: str
    ) -> None:
        """C4.7c — Les 6 nouvelles combinaisons (3 templates × 2 formats)
        sont toutes acceptées par Pydantic."""
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format=fmt,  # type: ignore[arg-type]
            template=tmpl,  # type: ignore[arg-type]
            options=DocumentGenerateOptions(
                subject="Test",
                level="Test Niveau",
                date_iso="2026-05-31",
            ),
        )
        assert body.format == fmt
        assert body.template == tmpl
        assert body.options.subject == "Test"

    @pytest.mark.parametrize(
        "invalid_format",
        ["both", "odt", "html", "pptx", "txt", ""],
    )
    def test_rejects_unknown_format_via_literal(self, invalid_format: str) -> None:
        with pytest.raises(ValidationError):
            DocumentGenerateRequest(
                conversation_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                format=invalid_format,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("valid_format", ["pdf", "docx"])
    def test_accepts_pdf_and_docx_formats(self, valid_format: str) -> None:
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format=valid_format,  # type: ignore[arg-type]
        )
        assert body.format == valid_format

    def test_docx_with_school_template_accepted(self) -> None:
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="docx",
            template="school",
            options=DocumentGenerateOptions(
                subject="Mathématiques",
                level="Terminale S",
            ),
        )
        assert body.format == "docx"
        assert body.template == "school"
        assert body.options.subject == "Mathématiques"

    def test_docx_with_minimal_template_accepted(self) -> None:
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            format="docx",
            template="minimal",
        )
        assert body.format == "docx"
        assert body.template == "minimal"

    def test_rejects_invalid_uuid_for_conversation_id(self) -> None:
        with pytest.raises(ValidationError):
            DocumentGenerateRequest(
                conversation_id="not-a-uuid",  # type: ignore[arg-type]
                message_id=uuid.uuid4(),
            )

    def test_rejects_title_exceeding_max_length(self) -> None:
        with pytest.raises(ValidationError):
            DocumentGenerateRequest(
                conversation_id=uuid.uuid4(),
                message_id=uuid.uuid4(),
                title="a" * 201,
            )

    def test_accepts_title_at_max_length(self) -> None:
        body = DocumentGenerateRequest(
            conversation_id=uuid.uuid4(),
            message_id=uuid.uuid4(),
            title="a" * 200,
        )
        assert body.title is not None
        assert len(body.title) == 200


# ──────────────────────────────────────────────────────────────────
# DocumentGenerateOptions
# ──────────────────────────────────────────────────────────────────


class TestDocumentGenerateOptionsSchema:
    def test_defaults(self) -> None:
        opts = DocumentGenerateOptions()
        assert opts.subject is None
        assert opts.level is None
        assert opts.date_iso is None
        assert opts.include_toc is False
        assert opts.page_numbers is True

    def test_full_options(self) -> None:
        opts = DocumentGenerateOptions(
            subject="SVT",
            level="3ème",
            date_iso="2026-05-30",
            include_toc=True,
            page_numbers=False,
        )
        assert opts.subject == "SVT"
        assert opts.level == "3ème"
        assert opts.date_iso == "2026-05-30"
        assert opts.include_toc is True
        assert opts.page_numbers is False

    def test_subject_max_length(self) -> None:
        with pytest.raises(ValidationError):
            DocumentGenerateOptions(subject="x" * 101)

    def test_level_max_length(self) -> None:
        with pytest.raises(ValidationError):
            DocumentGenerateOptions(level="y" * 101)


# ──────────────────────────────────────────────────────────────────
# DocumentGenerateResponse
# ──────────────────────────────────────────────────────────────────


class TestDocumentGenerateResponseSchema:
    def test_response_round_trip(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        lib_id = uuid.uuid4()
        resp = DocumentGenerateResponse(
            library_id=lib_id,
            download_url="https://example.com/foo.pdf?sig=abc",
            filename="document.pdf",
            size_bytes=12345,
            pages=10,
            truncated=False,
            expires_at=now,
            generated_at=now,
        )
        assert resp.library_id == lib_id
        assert resp.filename == "document.pdf"
        assert resp.size_bytes == 12345
        assert resp.pages == 10
        assert resp.truncated is False

    def test_response_with_truncated_flag(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        resp = DocumentGenerateResponse(
            library_id=uuid.uuid4(),
            download_url="https://example.com/x.pdf",
            filename="x.pdf",
            size_bytes=999_999,
            pages=50,
            truncated=True,
            expires_at=now,
            generated_at=now,
        )
        assert resp.truncated is True
        assert resp.pages == 50

    def test_negative_size_or_pages_rejected(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        with pytest.raises(ValidationError):
            DocumentGenerateResponse(
                library_id=uuid.uuid4(),
                download_url="https://example.com/x.pdf",
                filename="x.pdf",
                size_bytes=-1,
                pages=10,
                truncated=False,
                expires_at=now,
                generated_at=now,
            )
        with pytest.raises(ValidationError):
            DocumentGenerateResponse(
                library_id=uuid.uuid4(),
                download_url="https://example.com/x.pdf",
                filename="x.pdf",
                size_bytes=100,
                pages=-1,
                truncated=False,
                expires_at=now,
                generated_at=now,
            )
