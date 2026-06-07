"""Chargement Jinja2 + markdown→HTML (C4.7a).

Pipeline :
    1. markdown source → HTML safe via markdown-it-py (`html: False` désactivé)
    2. Jinja2 sandbox SelectAutoescape + FileSystemLoader(templates/) restreint
    3. Render template choisi (school/minimal) avec body_html déjà sanitisé

Sécurité :
    - Path traversal : `template_name` borné par Literal Pydantic en amont,
      ici on garde un check défensif via `TEMPLATES_REGISTRY`.
    - HTML injection : markdown-it-py avec `html: False` strip toute balise
      HTML inline dans le markdown source. Le `|safe` filter Jinja2 dans
      `_base.html` est appliqué UNIQUEMENT après ce strip.
    - SSRF : aucune URL fetching dans les templates V1 (les `<img src="...">`
      éventuels seraient ignorés par WeasyPrint en mode `assume_pretty_print`
      sans accès réseau — voir weasyprint_renderer.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from .branding import (
    BrandingContext,
    build_invisible_html_marker,
    build_pdf_branding_footer_css,
    build_pdf_branding_header_css,
)
from .exceptions import TemplateNotFoundError
from .schemas import DocumentGenerateOptions
from .watermark_assets import get_watermark_data_url

log = structlog.get_logger(__name__)

# ── Constantes module-level ──────────────────────────────────────────

TEMPLATES_DIR: Final[Path] = Path(__file__).parent / "templates"
"""Répertoire des templates Jinja2 (sibling du module Python)."""

ALLOWED_TEMPLATES: Final[frozenset[str]] = frozenset(
    {"school", "minimal", "sciences", "legal", "medicine"}
)
"""Whitelist stricte des templates disponibles (C4.7a + C4.7c).

Défense en profondeur : même si Pydantic Literal `DocumentTemplate` accepte
ces valeurs, on revérifie au moment du chargement (anti hot-reload + anti
dépendance accidentelle à un appelant non-Pydantic).

Les 5 templates correspondent à 5 fichiers `.html` dans `templates/` :
- school.html, minimal.html (C4.7a)
- sciences.html, legal.html, medicine.html (C4.7c)
"""


# ── Markdown renderer ────────────────────────────────────────────────


def _build_markdown_renderer() -> MarkdownIt:
    """Crée un MarkdownIt sécurisé (html: False).

    `html: False` strip toute balise HTML inline du markdown source.
    `breaks: True` convertit les retours ligne simples en `<br>` (UX
    cohérente avec ce que l'user voit dans le chat NEXYA).
    `linkify: True` auto-link les URL plain text.
    `typographer: True` smart quotes + tirets typographiques.
    """
    return MarkdownIt(
        "commonmark",
        {
            "html": False,
            "breaks": True,
            "linkify": True,
            "typographer": True,
        },
    )


_MD: Final[MarkdownIt] = _build_markdown_renderer()
"""Singleton MarkdownIt (thread-safe, pas de state mutable)."""


def render_markdown_to_html(markdown_source: str) -> str:
    """Rend markdown → HTML safe (pas de balise inline).

    Args:
        markdown_source: Texte markdown source (cap chars vérifié en amont
            par DocumentSourceTooLongError côté service).

    Returns:
        HTML string sanitisé (sans `<script>`, `<iframe>`, etc.).
    """
    if not markdown_source:
        return ""
    return _MD.render(markdown_source)


# ── Jinja2 environment ───────────────────────────────────────────────


def _build_jinja_env() -> Environment:
    """Crée un Environment Jinja2 sandbox.

    - `autoescape=select_autoescape(['html'])` : HTML escape par défaut.
    - `FileSystemLoader(TEMPLATES_DIR)` : restriction stricte au dossier
      templates (pas de `../` possible, le loader Jinja2 rejette les
      paths qui sortent de la base).
    - `trim_blocks=True` + `lstrip_blocks=True` : whitespace propre dans
      l'HTML rendu (anti-cascade WeasyPrint sur les espaces vides).
    """
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        # PAS de `bytecode_cache` V1 : les templates sont petits, le cache
        # disque serait plus coûteux que le re-parse (~1 ms par template).
    )


_JINJA_ENV: Final[Environment] = _build_jinja_env()
"""Singleton Jinja2 Environment (thread-safe en lecture)."""


# ── Public API ───────────────────────────────────────────────────────


def render_document_html(
    template_name: Literal["school", "minimal", "sciences", "legal", "medicine"],
    *,
    title: str | None,
    markdown_source: str,
    options: DocumentGenerateOptions,
    apply_watermark: bool = False,
    branding_context: BrandingContext | None = None,
) -> str:
    """Rend un template Jinja2 → HTML complet pour WeasyPrint.

    Args:
        template_name: Slug template (school | minimal | sciences | legal |
            medicine). Doit appartenir à `ALLOWED_TEMPLATES`.
        title: Titre principal affiché (h1). Si None, le template gère
            son défaut (« Devoir » pour school, omis pour minimal/sciences,
            « Document juridique » pour legal, « Document médical » pour
            medicine).
        markdown_source: Contenu source en markdown brut.
        options: Options de personnalisation par template (subject/level/
            date_iso réutilisés sémantiquement par template, cf. docstring
            de DocumentTemplate dans schemas.py).
        apply_watermark: True (défaut False) → injecte le logo NEXYA en
            base64 data URL dans le contexte Jinja2 (`watermark_data_url`).
            Le template `_base.html` ajoute alors une @page background-image
            bottom-right. C4.7d. Fail-safe : si l'asset PNG est introuvable,
            `watermark_data_url=None` et le template skip silencieusement
            (via `{% if watermark_data_url %}`).
        branding_context: BrandingContext (C4.8) — si fourni, injecte
            les blocs CSS @page top-left header `[NEXYA AI]` + @page
            bottom-center footer `Généré par NEXYA AI · Nexyalabs · date`
            + marqueur HTML invisible audit forensic. Si None (kill-switch
            OFF côté service), aucun branding visible (skip silencieux via
            `{% if branding_header_css %}` / `{% if branding_invisible_html %}`).

    Returns:
        HTML string prêt à passer à WeasyPrint.

    Raises:
        TemplateNotFoundError: Si `template_name` n'est pas dans la
            whitelist `ALLOWED_TEMPLATES`.
    """
    if template_name not in ALLOWED_TEMPLATES:
        log.warning(
            "documents.template.invalid",
            template=template_name,
            allowed=sorted(ALLOWED_TEMPLATES),
        )
        raise TemplateNotFoundError(
            f"Template '{template_name}' n'existe pas. "
            f"Templates disponibles : {sorted(ALLOWED_TEMPLATES)}."
        )

    # markdown → HTML safe (html: False côté markdown-it)
    body_html = render_markdown_to_html(markdown_source)

    # Date par défaut = aujourd'hui UTC ISO court (YYYY-MM-DD)
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")

    # C4.7d : résolution paresseuse du data URL watermark (singleton fail-safe).
    # `None` si apply_watermark=False ou asset PNG introuvable → template Jinja2
    # skip silencieusement via `{% if watermark_data_url %}`.
    watermark_data_url = get_watermark_data_url() if apply_watermark else None

    # C4.8 : branding CSS + marker invisible (skip silencieux si ctx None).
    # Fail-safe absolu : si les helpers raise (cas improbable, dataclass
    # frozen sans I/O), on laisse les chaînes vides → template skip via
    # `{% if branding_header_css %}`.
    branding_header_css = ""
    branding_footer_css = ""
    branding_invisible_html = ""
    if branding_context is not None:
        try:
            branding_header_css = build_pdf_branding_header_css(branding_context)
            branding_footer_css = build_pdf_branding_footer_css(branding_context)
            branding_invisible_html = build_invisible_html_marker(branding_context)
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "documents.branding.css_build_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                template=template_name,
            )
            # Reset les 3 strings à vide (pas de branding partiel)
            branding_header_css = ""
            branding_footer_css = ""
            branding_invisible_html = ""

    template = _JINJA_ENV.get_template(f"{template_name}.html")
    return template.render(
        title=title,
        body_html=body_html,
        options=options,
        today_iso=today_iso,
        watermark_data_url=watermark_data_url,
        branding_header_css=branding_header_css,
        branding_footer_css=branding_footer_css,
        branding_invisible_html=branding_invisible_html,
    )
