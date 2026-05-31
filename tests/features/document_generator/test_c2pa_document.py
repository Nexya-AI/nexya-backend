"""Tests unitaires — C2PA signature documents C4.7d (PDF + DOCX V1 skip).

Couvre :
    - _SUPPORTED_MIMES inclut application/pdf (anti-régression whitelist)
    - MockManifestProvider signe un PDF → applied=True + manifest_id mock-XXX
    - DOCX rejeté par C2PA (unsupported_format) → applied=False + skip_reason
    - Force_skip via Mock → applied=False + skip_reason informatif
    - 2 helpers internes (mocked) du module images/c2pa.py extension

Pattern strict aligné `tests/test_c2pa_provider_mock.py` E4.5.
Le RealC2PAProvider n'est PAS testé ici (clés X.509 hors-CI). Le test
d'intégration `service.py` côté `test_service.py` couvre le pipeline
complet avec MockManifestProvider mock-first auto (cf. settings.py).
"""

from __future__ import annotations

import pytest

from app.features.images.c2pa import (
    MockManifestProvider,
    _SUPPORTED_MIMES,
    C2PASignRequest,
)
from datetime import datetime, timezone


def _make_request(prompt: str = "Test doc") -> C2PASignRequest:
    """Helper factory pour un C2PASignRequest minimal valide."""
    return C2PASignRequest(
        prompt=prompt,
        provider="weasyprint",
        model="template_minimal",
        generation_timestamp=datetime.now(timezone.utc),
        watermark_applied=True,
        watermark_version="v1-doc-pdf-docx-2026-05",
    )


class TestC2PASupportedMimesIncludesPdf:
    """C4.7d — Anti-régression whitelist `_SUPPORTED_MIMES`."""

    def test_application_pdf_in_supported_mimes(self) -> None:
        """`application/pdf` est dans la whitelist depuis C4.7d."""
        assert "application/pdf" in _SUPPORTED_MIMES

    def test_image_mimes_still_supported_e4_5_preserved(self) -> None:
        """Anti-régression E4.5 — les 4 MIMEs image restent supportés."""
        assert "image/png" in _SUPPORTED_MIMES
        assert "image/jpeg" in _SUPPORTED_MIMES
        assert "image/jpg" in _SUPPORTED_MIMES
        assert "image/webp" in _SUPPORTED_MIMES

    def test_docx_mime_not_supported_explicit(self) -> None:
        """DOCX V1 NON supporté — différé V2 quand c2pa-rs supportera OOXML."""
        assert (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            not in _SUPPORTED_MIMES
        )


class TestMockSignsPdfHappyPath:
    """C4.7d — MockManifestProvider signe un PDF avec succès."""

    @pytest.mark.asyncio
    async def test_mock_signs_pdf_returns_applied_true(self) -> None:
        """Mock signe un PDF → applied=True + manifest_id mock-XXX."""
        provider = MockManifestProvider()
        fake_pdf_bytes = b"%PDF-1.4 fake bytes for test"
        result = await provider.sign_image(
            image_bytes=fake_pdf_bytes,
            mime_type="application/pdf",
            request=_make_request(),
        )
        assert result.applied is True
        assert result.manifest_id is not None
        assert result.manifest_id.startswith("mock-c2pa-")
        assert result.skip_reason is None
        assert result.signed_at is not None
        # Mock ne mute pas les bytes (bytes inchangés)
        assert result.image_bytes == fake_pdf_bytes

    @pytest.mark.asyncio
    async def test_mock_accumulates_calls_for_pdf(self) -> None:
        """Mock accumule les appels (test trace forensic)."""
        provider = MockManifestProvider()
        for _i in range(3):
            await provider.sign_image(
                image_bytes=b"%PDF-1.4 test",
                mime_type="application/pdf",
                request=_make_request(),
            )
        assert len(provider.calls) == 3
        for mime, _req in provider.calls:
            assert mime == "application/pdf"


class TestMockRejectsUnsupportedDocx:
    """C4.7d — DOCX rejeté par C2PA (skip_reason='unsupported_format')."""

    @pytest.mark.asyncio
    async def test_mock_docx_returns_unsupported_format(self) -> None:
        """DOCX mime → applied=False + skip_reason='unsupported_format'."""
        provider = MockManifestProvider()
        result = await provider.sign_image(
            image_bytes=b"PK fake docx zip",
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            request=_make_request(),
        )
        assert result.applied is False
        assert result.skip_reason == "unsupported_format"
        # Bytes inchangés (jamais muté en cas de skip)
        assert result.image_bytes == b"PK fake docx zip"
        assert result.manifest_id is None


class TestMockForceSkipForFailsafeTests:
    """C4.7d — force_skip=True permet tester le fail-safe côté caller."""

    @pytest.mark.asyncio
    async def test_force_skip_returns_applied_false_mock_force_skip(self) -> None:
        """force_skip=True → applied=False + skip_reason='mock_force_skip'."""
        provider = MockManifestProvider(force_skip=True)
        result = await provider.sign_image(
            image_bytes=b"%PDF-1.4 test",
            mime_type="application/pdf",
            request=_make_request(),
        )
        assert result.applied is False
        assert result.skip_reason == "mock_force_skip"
