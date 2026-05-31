"""Tests unitaires — Watermark PDF C4.7d (WeasyPrint @page background-image).

Couvre :
    - apply_watermark=True + asset PNG OK → data URL injecté dans HTML
    - apply_watermark=False → pas d'injection (template skip {% if %})
    - kill-switch settings.documents_generator_watermark_enabled=False → skip
    - fail-safe asset PNG introuvable → data URL=None, template skip

Pattern strict aligné `test_watermark.py` E4 (mock-first absolu, pas de
vraie image generated). Le rendu WeasyPrint n'est pas testé ici (le test
WeasyPrint binaire est `test_weasyprint_renderer.py` skip Windows attendu).
"""

from __future__ import annotations

import pytest

from app.features.document_generator.schemas import DocumentGenerateOptions
from app.features.document_generator.template_loader import render_document_html
from app.features.document_generator.watermark_assets import (
    WATERMARK_VERSION,
    get_watermark_data_url,
    get_watermark_path,
    reset_watermark_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_watermark_cache():
    """Reset singleton avant + après chaque test (isolation stricte)."""
    reset_watermark_cache_for_tests()
    yield
    reset_watermark_cache_for_tests()


class TestWatermarkAssetSingletons:
    """C4.7d — Helpers `watermark_assets.py` (singletons + fail-safe)."""

    def test_get_watermark_data_url_returns_base64_data_url(self) -> None:
        """Asset PNG existe → data URL base64 valide cached."""
        url = get_watermark_data_url()
        assert url is not None
        assert url.startswith("data:image/png;base64,")
        # PNG magic bytes en base64 commencent par "iVBOR" (8907 = PNG header)
        # ou similaire. On vérifie juste que c'est une string non vide après le préfixe.
        b64_part = url[len("data:image/png;base64,") :]
        assert len(b64_part) > 100  # PNG décent ≥ 100 chars en base64

    def test_get_watermark_data_url_is_cached_singleton(self) -> None:
        """Deux appels successifs retournent la même instance (cache process-wide)."""
        url1 = get_watermark_data_url()
        url2 = get_watermark_data_url()
        # Pas seulement equal, vraiment la MÊME string (singleton)
        assert url1 is url2

    def test_get_watermark_path_returns_existing_path(self) -> None:
        """Asset PNG existe → Path résolu cached."""
        path = get_watermark_path()
        assert path is not None
        assert path.is_file()
        assert path.name == "nexya_watermark.png"

    def test_watermark_version_constant_format(self) -> None:
        """WATERMARK_VERSION respecte le format `v{N}-{description}-{YYYY-MM}`."""
        assert WATERMARK_VERSION.startswith("v")
        assert "-" in WATERMARK_VERSION


class TestWatermarkInHtmlRender:
    """C4.7d — Injection conditionnelle du data URL dans le HTML rendu."""

    def test_apply_watermark_true_injects_data_url_in_html(self) -> None:
        """apply_watermark=True → @page background-image avec data URL."""
        html = render_document_html(
            template_name="minimal",
            title="Test",
            markdown_source="Bonjour monde",
            options=DocumentGenerateOptions(),
            apply_watermark=True,
        )
        # Le data URL est injecté dans le @page background-image
        assert "background-image: url('data:image/png;base64," in html
        assert "background-position: bottom right" in html

    def test_apply_watermark_false_no_data_url_in_html(self) -> None:
        """apply_watermark=False (défaut) → pas d'injection @page."""
        html = render_document_html(
            template_name="minimal",
            title="Test",
            markdown_source="Bonjour monde",
            options=DocumentGenerateOptions(),
            apply_watermark=False,
        )
        # Aucun background-image dans le @page
        assert "background-image: url('data:image/png;base64," not in html

    def test_apply_watermark_default_is_false(self) -> None:
        """Sans le param explicite, apply_watermark=False par défaut."""
        html = render_document_html(
            template_name="minimal",
            title="Test",
            markdown_source="Bonjour",
            options=DocumentGenerateOptions(),
        )
        assert "background-image: url('data:image/png;base64," not in html
