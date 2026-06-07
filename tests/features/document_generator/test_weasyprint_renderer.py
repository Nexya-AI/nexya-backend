"""Tests unitaires — weasyprint_renderer (C4.7a).

Mock-first strict : WeasyPrint et pikepdf sont monkeypatchés pour ne
pas dépendre des binaires système (cairo/pango) en CI.

Couvre :
    - render_html_to_pdf happy path (HTML → PDF bytes)
    - Timeout → DocumentRenderFailedError
    - WeasyPrint exception → DocumentRenderFailedError
    - Cap pages → truncated=True
    - HTML vide → DocumentRenderFailedError
    - URL fetcher refuse http:// (anti-SSRF)
"""

from __future__ import annotations

import asyncio

import pytest

from app.features.document_generator import weasyprint_renderer as renderer_module
from app.features.document_generator.exceptions import DocumentRenderFailedError
from app.features.document_generator.weasyprint_renderer import (
    _safe_url_fetcher,
    render_html_to_pdf,
)

# Mock PDF bytes minimaux (header PDF valide pour pikepdf)
_FAKE_PDF_BYTES = b"%PDF-1.4\nfake content\n%%EOF\n"


# ──────────────────────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────────────────────


class TestRenderHappy:
    @pytest.mark.asyncio
    async def test_happy_path_returns_rendered_pdf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Render HTML → PDF avec mock complet."""

        def fake_render_sync(html_content: str) -> bytes:
            assert "<html" in html_content
            return _FAKE_PDF_BYTES

        def fake_post_process(pdf_bytes: bytes, *, max_pages: int, branding_context=None):
            from app.features.document_generator.weasyprint_renderer import RenderedPdf

            return RenderedPdf(
                pdf_bytes=pdf_bytes,
                pages=5,
                truncated=False,
                size_bytes=len(pdf_bytes),
            )

        monkeypatch.setattr(renderer_module, "_render_pdf_sync", fake_render_sync)
        monkeypatch.setattr(renderer_module, "_post_process_pdf_sync", fake_post_process)

        result = await render_html_to_pdf("<html><body>hello</body></html>")
        assert result.pages == 5
        assert result.truncated is False
        assert result.pdf_bytes == _FAKE_PDF_BYTES
        assert result.size_bytes == len(_FAKE_PDF_BYTES)


# ──────────────────────────────────────────────────────────────────
# Timeout
# ──────────────────────────────────────────────────────────────────


class TestRenderTimeout:
    @pytest.mark.asyncio
    async def test_timeout_raises_document_render_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si WeasyPrint dépasse le timeout → DocumentRenderFailedError."""

        async def fake_to_thread(fn, *args, **kwargs):
            await asyncio.sleep(10)  # Bien plus que le timeout du test
            return _FAKE_PDF_BYTES

        monkeypatch.setattr(renderer_module.asyncio, "to_thread", fake_to_thread)

        with pytest.raises(DocumentRenderFailedError) as exc_info:
            await render_html_to_pdf(
                "<html><body>x</body></html>",
                timeout_seconds=0.1,
            )
        assert "timeout" in str(exc_info.value).lower()
        assert exc_info.value.code == "DOCUMENT_RENDER_FAILED"


# ──────────────────────────────────────────────────────────────────
# Exceptions WeasyPrint
# ──────────────────────────────────────────────────────────────────


class TestRenderExceptions:
    @pytest.mark.asyncio
    async def test_weasyprint_exception_mapped_to_document_render_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception WeasyPrint (cairo, layout) → DocumentRenderFailedError."""

        def fake_render_sync(html_content: str) -> bytes:
            raise RuntimeError("cairo failed: invalid layout")

        monkeypatch.setattr(renderer_module, "_render_pdf_sync", fake_render_sync)

        with pytest.raises(DocumentRenderFailedError) as exc_info:
            await render_html_to_pdf("<html><body>x</body></html>")
        assert exc_info.value.code == "DOCUMENT_RENDER_FAILED"

    @pytest.mark.asyncio
    async def test_pikepdf_exception_mapped_to_document_render_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception pikepdf (PDF corrompu) → DocumentRenderFailedError."""

        def fake_render_sync(html_content: str) -> bytes:
            return _FAKE_PDF_BYTES

        def fake_post_process(pdf_bytes: bytes, *, max_pages: int, branding_context=None):
            raise ValueError("PDF malformed at offset 42")

        monkeypatch.setattr(renderer_module, "_render_pdf_sync", fake_render_sync)
        monkeypatch.setattr(renderer_module, "_post_process_pdf_sync", fake_post_process)

        with pytest.raises(DocumentRenderFailedError) as exc_info:
            await render_html_to_pdf("<html><body>x</body></html>")
        assert (
            "compression" in str(exc_info.value).lower()
            or exc_info.value.code == "DOCUMENT_RENDER_FAILED"
        )


# ──────────────────────────────────────────────────────────────────
# Cap pages → truncated
# ──────────────────────────────────────────────────────────────────


class TestRenderTruncation:
    @pytest.mark.asyncio
    async def test_pdf_truncated_flag_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Si pikepdf tronque les pages, truncated=True dans le résultat."""

        def fake_render_sync(html_content: str) -> bytes:
            return _FAKE_PDF_BYTES

        def fake_post_process(pdf_bytes: bytes, *, max_pages: int, branding_context=None):
            from app.features.document_generator.weasyprint_renderer import RenderedPdf

            return RenderedPdf(
                pdf_bytes=pdf_bytes,
                pages=max_pages,
                truncated=True,
                size_bytes=len(pdf_bytes),
            )

        monkeypatch.setattr(renderer_module, "_render_pdf_sync", fake_render_sync)
        monkeypatch.setattr(renderer_module, "_post_process_pdf_sync", fake_post_process)

        result = await render_html_to_pdf(
            "<html><body>x</body></html>",
            max_pages=50,
        )
        assert result.truncated is True
        assert result.pages == 50


# ──────────────────────────────────────────────────────────────────
# Edge cases — HTML vide
# ──────────────────────────────────────────────────────────────────


class TestRenderEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_html_rejected(self) -> None:
        with pytest.raises(DocumentRenderFailedError) as exc_info:
            await render_html_to_pdf("")
        assert (
            "vide" in str(exc_info.value).lower() or exc_info.value.code == "DOCUMENT_RENDER_FAILED"
        )

    @pytest.mark.asyncio
    async def test_whitespace_only_html_rejected(self) -> None:
        with pytest.raises(DocumentRenderFailedError):
            await render_html_to_pdf("   \n\t  ")


# ──────────────────────────────────────────────────────────────────
# URL fetcher anti-SSRF
# ──────────────────────────────────────────────────────────────────


class TestSafeUrlFetcher:
    def test_http_url_blocked_returns_empty(self) -> None:
        """Anti-SSRF : http:// refuse silencieusement."""
        result = _safe_url_fetcher("http://evil.com/x.png")
        assert result["string"] == b""

    def test_https_url_blocked_returns_empty(self) -> None:
        result = _safe_url_fetcher("https://anywhere.com/x.png")
        assert result["string"] == b""

    def test_file_url_blocked_returns_empty(self) -> None:
        """Anti file:// — pas de read local disk."""
        result = _safe_url_fetcher("file:///etc/passwd")
        assert result["string"] == b""

    def test_aws_metadata_url_blocked(self) -> None:
        """Anti-SSRF AWS metadata endpoint critique."""
        result = _safe_url_fetcher("http://169.254.169.254/latest/meta-data/")
        assert result["string"] == b""

    def test_data_uri_is_delegated_to_weasyprint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """data: URIs sont délégués au default_url_fetcher WeasyPrint.

        Skip si WeasyPrint runtime KO (binaires cairo/pango manquants
        côté Windows dev). En CI Linux + prod Linux, ces deps sont
        installées via apt et le test passe.
        """
        # `ImportError` ne suffit pas — WeasyPrint lève `OSError` au
        # module init si libgobject-2.0-0 / cairo manquent (Windows).
        try:
            from weasyprint import default_url_fetcher  # noqa: F401
        except (ImportError, OSError) as exc:
            pytest.skip(f"weasyprint runtime indisponible : {exc}")

        # Mini data URI valide (1px PNG transparent)
        data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
        result = _safe_url_fetcher(data_uri)
        # Default fetcher devrait décoder le data: URI
        assert isinstance(result, dict)
