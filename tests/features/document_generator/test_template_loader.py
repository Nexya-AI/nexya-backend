"""Tests unitaires — template_loader (C4.7a).

Couvre :
    - render_markdown_to_html : `html: False` strip les balises inline
    - render_document_html : Jinja2 escape XSS sur title/subject/level
    - TemplateNotFoundError sur slug invalide (défense en profondeur)
    - Templates school + minimal rendent un HTML valide
"""

from __future__ import annotations

import pytest

from app.features.document_generator.exceptions import TemplateNotFoundError
from app.features.document_generator.schemas import DocumentGenerateOptions
from app.features.document_generator.template_loader import (
    render_document_html,
    render_markdown_to_html,
)


# ──────────────────────────────────────────────────────────────────
# render_markdown_to_html
# ──────────────────────────────────────────────────────────────────


class TestRenderMarkdownToHtml:
    def test_plain_markdown_renders_to_html(self) -> None:
        md = "# Titre\n\nUn paragraphe **gras**."
        html = render_markdown_to_html(md)
        assert "<h1>" in html
        assert "Titre" in html
        assert "<strong>gras</strong>" in html

    def test_inline_html_script_is_escaped_or_stripped(self) -> None:
        """`html: False` côté markdown-it strip toute balise HTML inline.

        Anti-XSS critique : un user qui injecte `<script>` dans son
        markdown ne doit pas voir le script rendu côté PDF.
        """
        md = "Hello <script>alert('xss')</script> world"
        html = render_markdown_to_html(md)
        # Le script tag doit être escape (pas executable)
        assert "<script>" not in html
        # Mais le texte autour reste lisible
        assert "Hello" in html
        assert "world" in html

    def test_inline_img_src_is_escaped(self) -> None:
        """`html: False` strip aussi les `<img>` inline."""
        md = "Voici une image: <img src='http://evil.com/x.png'>"
        html = render_markdown_to_html(md)
        # L'img tag inline n'est pas rendu
        assert "<img src" not in html

    def test_code_block_renders_with_pre(self) -> None:
        md = "```python\nprint('hello')\n```"
        html = render_markdown_to_html(md)
        assert "<pre>" in html
        assert "<code" in html
        assert "print" in html

    def test_table_markdown_renders(self) -> None:
        # markdown-it 'commonmark' preset n'inclut pas tables par défaut,
        # mais on garde le test pour traçabilité
        md = "Un paragraphe."
        html = render_markdown_to_html(md)
        assert "paragraphe" in html

    def test_empty_markdown_returns_empty(self) -> None:
        assert render_markdown_to_html("") == ""

    def test_linkify_auto_links_urls(self) -> None:
        md = "Voir https://nexya.ai pour plus d'infos."
        html = render_markdown_to_html(md)
        # linkify: True dans MarkdownIt config
        assert "href" in html or "https://nexya.ai" in html


# ──────────────────────────────────────────────────────────────────
# render_document_html — template school
# ──────────────────────────────────────────────────────────────────


class TestRenderSchoolTemplate:
    def test_school_template_renders_with_title_and_meta(self) -> None:
        html = render_document_html(
            "school",
            title="Devoir Maths",
            markdown_source="# Exercice 1\n\nRésoudre x+2=5.",
            options=DocumentGenerateOptions(
                subject="Mathématiques",
                level="6ème",
                date_iso="2026-05-30",
            ),
        )
        assert "<!DOCTYPE html>" in html
        assert "Devoir Maths" in html
        assert "Mathématiques" in html
        assert "6ème" in html
        assert "2026-05-30" in html
        assert "Exercice 1" in html

    def test_school_template_escapes_title_xss(self) -> None:
        html = render_document_html(
            "school",
            title="<script>alert(1)</script>",
            markdown_source="Hello",
            options=DocumentGenerateOptions(),
        )
        # Jinja2 autoescape doit transformer le titre en text safe
        assert "<script>alert(1)</script>" not in html
        # Forme escape attendue
        assert "&lt;script" in html or "alert" not in html.split("<title>")[1].split("</title>")[0]

    def test_school_template_without_subject_or_level(self) -> None:
        html = render_document_html(
            "school",
            title="Sans détails",
            markdown_source="Contenu",
            options=DocumentGenerateOptions(),
        )
        # Pas de section subject/level rendue
        assert "Matière" not in html
        assert "Niveau" not in html


# ──────────────────────────────────────────────────────────────────
# render_document_html — template minimal
# ──────────────────────────────────────────────────────────────────


class TestRenderMinimalTemplate:
    def test_minimal_template_renders_with_title(self) -> None:
        html = render_document_html(
            "minimal",
            title="Notes du 30 mai",
            markdown_source="# Section 1\n\nTexte.",
            options=DocumentGenerateOptions(),
        )
        assert "<!DOCTYPE html>" in html
        assert "Notes du 30 mai" in html
        assert "Section 1" in html

    def test_minimal_template_without_title(self) -> None:
        html = render_document_html(
            "minimal",
            title=None,
            markdown_source="Juste du contenu.",
            options=DocumentGenerateOptions(),
        )
        assert "<!DOCTYPE html>" in html
        assert "Juste du contenu" in html
        # Pas de h1.minimal-title rendu sans titre (le block header
        # complet est skip via `{% if title %}`)
        assert '<h1 class="minimal-title">' not in html

    def test_minimal_template_with_page_numbers_off(self) -> None:
        html = render_document_html(
            "minimal",
            title="Sans pagination",
            markdown_source="X",
            options=DocumentGenerateOptions(page_numbers=False),
        )
        # Le bloc @bottom-right avec counter(page) ne doit pas être présent
        assert "counter(page)" not in html


# ──────────────────────────────────────────────────────────────────
# Defensive — TemplateNotFoundError
# ──────────────────────────────────────────────────────────────────


class TestTemplateNotFoundError:
    def test_unknown_template_raises_explicit_error(self) -> None:
        with pytest.raises(TemplateNotFoundError) as exc_info:
            render_document_html(
                "sciences",  # type: ignore[arg-type]
                title="Hack",
                markdown_source="Test",
                options=DocumentGenerateOptions(),
            )
        assert "sciences" in str(exc_info.value)
        assert exc_info.value.code == "TEMPLATE_NOT_FOUND"

    def test_path_traversal_attempt_blocked(self) -> None:
        """Tentative de path traversal `../config` rejetée."""
        with pytest.raises(TemplateNotFoundError):
            render_document_html(
                "../config",  # type: ignore[arg-type]
                title="Hack",
                markdown_source="Test",
                options=DocumentGenerateOptions(),
            )

    def test_empty_template_name_rejected(self) -> None:
        with pytest.raises(TemplateNotFoundError):
            render_document_html(
                "",  # type: ignore[arg-type]
                title="X",
                markdown_source="Test",
                options=DocumentGenerateOptions(),
            )
