"""Tests unitaires — docx_renderer (C4.7b).

Pipeline strict : markdown-it AST → python-docx natif → bytes BytesIO.

Couvre :
    - render_markdown_to_docx happy path (markdown → DOCX bytes valides)
    - Templates school + minimal (variantes header)
    - Timeout → DocumentRenderFailedError
    - Exception python-docx → DocumentRenderFailedError
    - Cap pages → truncated=True
    - Markdown vide → DocumentRenderFailedError
    - Estimation pages heuristique (paragraphes / page)
"""

from __future__ import annotations

import asyncio
import io

import pytest

from app.features.document_generator import docx_renderer as renderer_module
from app.features.document_generator.docx_renderer import (
    RenderedDocx,
    render_markdown_to_docx,
)
from app.features.document_generator.exceptions import DocumentRenderFailedError
from app.features.document_generator.schemas import DocumentGenerateOptions


# ──────────────────────────────────────────────────────────────────
# Happy path — Template minimal
# ──────────────────────────────────────────────────────────────────


class TestRenderHappyMinimal:
    @pytest.mark.asyncio
    async def test_minimal_renders_valid_docx_bytes(self) -> None:
        result = await render_markdown_to_docx(
            template_name="minimal",
            title="Mon document",
            markdown_source="# Titre\n\nUn paragraphe simple.",
            options=DocumentGenerateOptions(),
        )
        assert isinstance(result, RenderedDocx)
        assert len(result.docx_bytes) > 0
        # DOCX = ZIP, donc commence par "PK\x03\x04"
        assert result.docx_bytes[:2] == b"PK"
        assert result.size_bytes == len(result.docx_bytes)
        assert result.pages >= 1
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_minimal_with_no_title_renders(self) -> None:
        result = await render_markdown_to_docx(
            template_name="minimal",
            title=None,
            markdown_source="Juste du texte sans titre.",
            options=DocumentGenerateOptions(),
        )
        assert len(result.docx_bytes) > 0
        assert result.pages >= 1

    @pytest.mark.asyncio
    async def test_minimal_with_complex_markdown(self) -> None:
        md = """# Header 1

## Header 2

Un **paragraphe** avec du *italique* et du `code inline`.

- Item 1
- Item 2
- Item 3

```python
def hello():
    return "world"
```

> Une citation.

---

Fin du document.
"""
        result = await render_markdown_to_docx(
            template_name="minimal",
            title="Doc complexe",
            markdown_source=md,
            options=DocumentGenerateOptions(),
        )
        assert len(result.docx_bytes) > 1000  # DOCX riche, pas une coquille vide


# ──────────────────────────────────────────────────────────────────
# Happy path — Template school
# ──────────────────────────────────────────────────────────────────


class TestRenderHappySchool:
    @pytest.mark.asyncio
    async def test_school_with_subject_level_date(self) -> None:
        result = await render_markdown_to_docx(
            template_name="school",
            title="Devoir maison",
            markdown_source="Énoncé du devoir...",
            options=DocumentGenerateOptions(
                subject="Mathématiques",
                level="Terminale S",
                date_iso="2026-05-30",
            ),
        )
        assert len(result.docx_bytes) > 0
        assert result.docx_bytes[:2] == b"PK"

    @pytest.mark.asyncio
    async def test_school_without_options_uses_defaults(self) -> None:
        result = await render_markdown_to_docx(
            template_name="school",
            title="Travail simple",
            markdown_source="Contenu...",
            options=DocumentGenerateOptions(),
        )
        assert len(result.docx_bytes) > 0


# ──────────────────────────────────────────────────────────────────
# Timeout
# ──────────────────────────────────────────────────────────────────


class TestRenderTimeout:
    @pytest.mark.asyncio
    async def test_timeout_raises_document_render_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Si python-docx dépasse le timeout → DocumentRenderFailedError."""

        async def fake_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(10)
            return b"never"

        monkeypatch.setattr(renderer_module.asyncio, "to_thread", fake_to_thread)

        with pytest.raises(DocumentRenderFailedError) as exc_info:
            await render_markdown_to_docx(
                template_name="minimal",
                title="x",
                markdown_source="hello",
                options=DocumentGenerateOptions(),
                timeout_seconds=0.1,
            )
        assert "timeout" in str(exc_info.value).lower()
        assert exc_info.value.code == "DOCUMENT_RENDER_FAILED"


# ──────────────────────────────────────────────────────────────────
# Exceptions python-docx
# ──────────────────────────────────────────────────────────────────


class TestRenderExceptions:
    @pytest.mark.asyncio
    async def test_docx_exception_mapped_to_document_render_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception python-docx → DocumentRenderFailedError."""

        def fake_render_sync(**kwargs) -> RenderedDocx:  # type: ignore[no-untyped-def]
            raise RuntimeError("Office Open XML struct invalid")

        monkeypatch.setattr(renderer_module, "_render_docx_sync", fake_render_sync)

        with pytest.raises(DocumentRenderFailedError) as exc_info:
            await render_markdown_to_docx(
                template_name="minimal",
                title="x",
                markdown_source="hello",
                options=DocumentGenerateOptions(),
            )
        assert exc_info.value.code == "DOCUMENT_RENDER_FAILED"


# ──────────────────────────────────────────────────────────────────
# Cap pages → truncated
# ──────────────────────────────────────────────────────────────────


class TestRenderTruncation:
    @pytest.mark.asyncio
    async def test_truncated_flag_when_cap_pages_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Avec max_pages=1 et beaucoup de paragraphes → truncated=True."""

        # 100 paragraphes (cap = 40 paragraphes/page × 1 page = 40 paragraphes max)
        big_md = "\n\n".join(f"Paragraphe numéro {i}" for i in range(100))

        result = await render_markdown_to_docx(
            template_name="minimal",
            title="Long doc",
            markdown_source=big_md,
            options=DocumentGenerateOptions(),
            max_pages=1,
        )
        assert result.truncated is True
        assert result.pages == 1
        assert len(result.docx_bytes) > 0


# ──────────────────────────────────────────────────────────────────
# Edge cases — markdown vide
# ──────────────────────────────────────────────────────────────────


class TestRenderEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_markdown_rejected(self) -> None:
        with pytest.raises(DocumentRenderFailedError):
            await render_markdown_to_docx(
                template_name="minimal",
                title="x",
                markdown_source="",
                options=DocumentGenerateOptions(),
            )

    @pytest.mark.asyncio
    async def test_whitespace_only_markdown_rejected(self) -> None:
        with pytest.raises(DocumentRenderFailedError):
            await render_markdown_to_docx(
                template_name="minimal",
                title="x",
                markdown_source="   \n\t  ",
                options=DocumentGenerateOptions(),
            )


# ──────────────────────────────────────────────────────────────────
# RenderedDocx dataclass
# ──────────────────────────────────────────────────────────────────


class TestRenderedDocxDataclass:
    def test_frozen_dataclass(self) -> None:
        result = RenderedDocx(
            docx_bytes=b"PK\x03\x04fake",
            pages=5,
            truncated=False,
            size_bytes=10,
        )
        with pytest.raises((AttributeError, Exception)):
            result.pages = 99  # type: ignore[misc]

    def test_size_matches_bytes_length(self) -> None:
        data = b"PK\x03\x04" + b"x" * 1000
        result = RenderedDocx(
            docx_bytes=data,
            pages=2,
            truncated=False,
            size_bytes=len(data),
        )
        assert result.size_bytes == len(result.docx_bytes)


# ──────────────────────────────────────────────────────────────────
# Heuristique pages (sync helper)
# ──────────────────────────────────────────────────────────────────


class TestPagesEstimation:
    @pytest.mark.asyncio
    async def test_short_doc_estimates_one_page(self) -> None:
        result = await render_markdown_to_docx(
            template_name="minimal",
            title="Court",
            markdown_source="Un seul paragraphe.",
            options=DocumentGenerateOptions(),
        )
        assert result.pages == 1

    @pytest.mark.asyncio
    async def test_medium_doc_estimates_multiple_pages(self) -> None:
        # 80 paragraphes ≈ 2 pages (40 par page)
        md = "\n\n".join(f"Paragraphe {i}" for i in range(80))
        result = await render_markdown_to_docx(
            template_name="minimal",
            title="Moyen",
            markdown_source=md,
            options=DocumentGenerateOptions(),
            max_pages=10,
        )
        assert result.pages >= 2
        assert result.truncated is False


# ──────────────────────────────────────────────────────────────────
# DOCX structure validation (ZIP signature + entrées attendues)
# ──────────────────────────────────────────────────────────────────


class TestDocxStructure:
    @pytest.mark.asyncio
    async def test_docx_is_valid_zip_with_word_document(self) -> None:
        import zipfile

        result = await render_markdown_to_docx(
            template_name="minimal",
            title="Validation",
            markdown_source="# Hello\n\nMonde.",
            options=DocumentGenerateOptions(),
        )

        # Lire le DOCX comme ZIP et vérifier la présence de word/document.xml
        with zipfile.ZipFile(io.BytesIO(result.docx_bytes)) as zf:
            namelist = zf.namelist()
            assert "word/document.xml" in namelist
            assert "[Content_Types].xml" in namelist
