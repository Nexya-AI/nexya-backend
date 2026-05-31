"""python-docx renderer (C4.7b — Word natif).

Pipeline strict :
    1. Markdown source → AST tokens via markdown-it-py (singleton C4.7a)
    2. Traversal AST → python-docx natif (paragraphes + styles)
    3. Comptage pages estimé via heuristique paragraphes
    4. Save en BytesIO → bytes finaux

Sécurité :
    - Anti CPU exhaust : timeout 30s via asyncio.wait_for
    - Anti DOCX géant : cap 50 pages estimé (heuristique conservative)
    - Anti path traversal : aucune lecture disque, tout en mémoire
    - Anti XSS : markdown-it html: False (cf. template_loader.py)

Fail-safe :
    - Exception python-docx (struct OOXML invalide) → DocumentRenderFailedError
    - Timeout asyncio → DocumentRenderFailedError (logué + 503)

Limitations V1 documentées :
    - Pas d'images inline (les data: URIs markdown sont skip avec note italique)
    - Pas de tables avancées (markdown-it standard ne produit pas de table token V1)
    - Pas d'hyperlinks cliquables (texte brut conservé, lien en italique gris)
    - Templates école/minimal partagent le même renderer (variantes header)
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal

import structlog

from .exceptions import DocumentRenderFailedError
from .schemas import DocumentGenerateOptions
from .template_loader import _MD  # Singleton MarkdownIt partagé avec PDF

log = structlog.get_logger(__name__)

# ── Constantes module-level ──────────────────────────────────────────

_DEFAULT_RENDER_TIMEOUT_SECONDS: Final[float] = 30.0
"""Timeout hardcap render python-docx (anti CPU exhaust)."""

_DEFAULT_MAX_PAGES: Final[int] = 50
"""Cap pages dur estimé (heuristique 40 paragraphes / page)."""

_PARAGRAPHS_PER_PAGE_ESTIMATE: Final[int] = 40
"""Heuristique conservative : ~40 paragraphes courts = 1 page A4."""


# ── Dataclass de retour ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RenderedDocx:
    """Résultat du rendu DOCX (output binaire + metadata).

    Attributes:
        docx_bytes: Contenu DOCX binaire final.
        pages: Nombre estimé de pages (heuristique).
        truncated: True si le DOCX a été tronqué au cap pages.
        size_bytes: len(docx_bytes) — exposé pour cohérence response.
    """

    docx_bytes: bytes
    pages: int
    truncated: bool
    size_bytes: int


# ── Helpers sync (appelés dans to_thread) ────────────────────────────


def _add_styled_run(paragraph: Any, text: str, *, bold: bool = False, italic: bool = False, code: bool = False) -> None:
    """Ajoute un run de texte avec styles inline."""
    if not text:
        return
    run = paragraph.add_run(text)
    if bold:
        run.bold = True
    if italic:
        run.italic = True
    if code:
        # Police monospace pour le code inline
        run.font.name = "Consolas"


def _render_inline_tokens(paragraph: Any, tokens: list[Any]) -> None:
    """Traverse les tokens inline d'un paragraphe et applique les styles.

    markdown-it génère des tokens inline en pile : on suit `strong_open`,
    `em_open`, `code_inline`, `text`, etc. et on accumule les styles
    actifs dans un stack.
    """
    bold_depth = 0
    italic_depth = 0

    for tok in tokens:
        tok_type = tok.type
        if tok_type == "strong_open":
            bold_depth += 1
        elif tok_type == "strong_close":
            bold_depth = max(0, bold_depth - 1)
        elif tok_type == "em_open":
            italic_depth += 1
        elif tok_type == "em_close":
            italic_depth = max(0, italic_depth - 1)
        elif tok_type == "text":
            _add_styled_run(
                paragraph,
                tok.content,
                bold=bold_depth > 0,
                italic=italic_depth > 0,
            )
        elif tok_type == "code_inline":
            _add_styled_run(paragraph, tok.content, code=True)
        elif tok_type == "softbreak" or tok_type == "hardbreak":
            paragraph.add_run("\n")
        elif tok_type == "link_open":
            # V1 : lien rendu en italique gris (pas de hyperlink natif)
            italic_depth += 1
        elif tok_type == "link_close":
            italic_depth = max(0, italic_depth - 1)
        # Autres tokens (image, html_inline) silencieusement ignorés V1


def _render_tokens_to_docx(document: Any, tokens: list[Any], *, max_paragraphs: int) -> tuple[int, bool]:
    """Traverse les tokens markdown-it et écrit dans le document python-docx.

    Returns:
        Tuple (paragraphs_written, truncated).
    """
    paragraphs_written = 0
    truncated = False
    i = 0
    n = len(tokens)

    while i < n:
        if paragraphs_written >= max_paragraphs:
            truncated = True
            break

        tok = tokens[i]
        tok_type = tok.type

        # ── Headings (h1-h6) ──────────────────────────────────────
        if tok_type == "heading_open":
            level = int(tok.tag[1])  # h1 → 1, h2 → 2
            # Le token suivant est 'inline' avec le contenu
            if i + 1 < n and tokens[i + 1].type == "inline":
                inline = tokens[i + 1]
                heading = document.add_heading(level=min(level, 9))
                _render_inline_tokens(heading, inline.children or [])
                paragraphs_written += 1
            i += 3  # heading_open + inline + heading_close
            continue

        # ── Paragraphes ───────────────────────────────────────────
        if tok_type == "paragraph_open":
            if i + 1 < n and tokens[i + 1].type == "inline":
                inline = tokens[i + 1]
                p = document.add_paragraph()
                _render_inline_tokens(p, inline.children or [])
                paragraphs_written += 1
            i += 3
            continue

        # ── Code blocks ───────────────────────────────────────────
        if tok_type == "fence" or tok_type == "code_block":
            # Style "Intense Quote" par défaut python-docx
            p = document.add_paragraph()
            run = p.add_run(tok.content.rstrip("\n"))
            run.font.name = "Consolas"
            run.font.size = None  # Hérite du style par défaut
            paragraphs_written += 1
            i += 1
            continue

        # ── Listes (bullet + ordered) ─────────────────────────────
        if tok_type == "bullet_list_open" or tok_type == "ordered_list_open":
            style_name = "List Bullet" if tok_type == "bullet_list_open" else "List Number"
            # On itère jusqu'au _close correspondant
            list_depth = 1
            i += 1
            while i < n and list_depth > 0 and paragraphs_written < max_paragraphs:
                inner = tokens[i]
                if inner.type in ("bullet_list_open", "ordered_list_open"):
                    list_depth += 1
                elif inner.type in ("bullet_list_close", "ordered_list_close"):
                    list_depth -= 1
                elif inner.type == "list_item_open":
                    # Token suivant : paragraph_open → inline
                    if i + 1 < n and tokens[i + 1].type == "paragraph_open":
                        if i + 2 < n and tokens[i + 2].type == "inline":
                            inline = tokens[i + 2]
                            try:
                                p = document.add_paragraph(style=style_name)
                            except KeyError:
                                # Style non disponible → fallback paragraphe simple
                                p = document.add_paragraph()
                            _render_inline_tokens(p, inline.children or [])
                            paragraphs_written += 1
                i += 1
            if paragraphs_written >= max_paragraphs:
                truncated = True
                break
            continue

        # ── Blockquote ────────────────────────────────────────────
        if tok_type == "blockquote_open":
            # On collecte les paragraphes internes en italique
            i += 1
            while i < n and tokens[i].type != "blockquote_close":
                if tokens[i].type == "inline":
                    p = document.add_paragraph()
                    p.paragraph_format.left_indent = None  # Indentation par défaut
                    inline = tokens[i]
                    for child in inline.children or []:
                        if child.type == "text":
                            _add_styled_run(p, child.content, italic=True)
                    paragraphs_written += 1
                    if paragraphs_written >= max_paragraphs:
                        truncated = True
                        break
                i += 1
            i += 1  # Skip blockquote_close
            continue

        # ── Horizontal rule ───────────────────────────────────────
        if tok_type == "hr":
            p = document.add_paragraph("─" * 40)
            paragraphs_written += 1
            i += 1
            continue

        # Token inconnu : skip silencieux
        i += 1

    return paragraphs_written, truncated


def _build_school_header(document: Any, *, title: str | None, options: DocumentGenerateOptions, today_iso: str) -> None:
    """Ajoute l'en-tête School (titre centré + métadonnées italique)."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    # Titre principal centré
    if title:
        h = document.add_heading(title, level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Métadonnées italique gris
    meta_lines = []
    if options.subject:
        meta_lines.append(f"Matière : {options.subject}")
    if options.level:
        meta_lines.append(f"Niveau : {options.level}")
    meta_lines.append(f"Date : {options.date_iso or today_iso}")

    if meta_lines:
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for idx, line in enumerate(meta_lines):
            if idx > 0:
                p.add_run(" · ")
            run = p.add_run(line)
            run.italic = True

    # Espace avant le body
    document.add_paragraph()


def _build_minimal_header(document: Any, *, title: str | None) -> None:
    """Ajoute l'en-tête Minimal (titre H1 si présent)."""
    if title:
        document.add_heading(title, level=1)


def _build_sciences_header(
    document: Any,
    *,
    title: str | None,
    options: DocumentGenerateOptions,
    today_iso: str,
) -> None:
    """Ajoute l'en-tête Sciences (titre centré + meta italique gris).

    Style sobre académique aligné `templates/sciences.html` C4.7c.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    if title:
        h = document.add_heading(title, level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Meta : Discipline · Établissement · Date (italique gris)
    meta_parts: list[str] = []
    if options.subject:
        meta_parts.append(options.subject)
    if options.level:
        meta_parts.append(options.level)
    meta_parts.append(options.date_iso or today_iso)

    if meta_parts:
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(" · ".join(meta_parts))
        run.italic = True

    # Espace avant body
    document.add_paragraph()


def _build_legal_header(
    document: Any,
    *,
    title: str | None,
    options: DocumentGenerateOptions,
    today_iso: str,
) -> None:
    """Ajoute l'en-tête Legal (gauche-aligné + meta labellisée).

    Style juridique formel aligné `templates/legal.html` C4.7c (sans
    serif Georgia côté DOCX car python-docx ne change pas la police
    par défaut sans manipuler XML — V1 utilise la police par défaut).
    """
    if title:
        document.add_heading(title, level=0)

    # Meta : labels Domaine / Juridiction / Date
    if options.subject:
        p = document.add_paragraph()
        run_label = p.add_run("Domaine : ")
        run_label.bold = True
        p.add_run(options.subject)

    if options.level:
        p = document.add_paragraph()
        run_label = p.add_run("Juridiction : ")
        run_label.bold = True
        p.add_run(options.level)

    p = document.add_paragraph()
    run_label = p.add_run("Date : ")
    run_label.bold = True
    p.add_run(options.date_iso or today_iso)

    # Espace avant body
    document.add_paragraph()


def _build_medicine_header(
    document: Any,
    *,
    title: str | None,
    options: DocumentGenerateOptions,
    today_iso: str,
) -> None:
    """Ajoute l'en-tête Medicine (titre centré bleu + meta).

    Style sobre médical aligné `templates/medicine.html` C4.7c.
    Le disclaimer SAFETY-CRITICAL est ajouté séparément via
    `_build_medicine_disclaimer_paragraph` AVANT le body.
    """
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import RGBColor

    if title:
        h = document.add_heading(title, level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        # Titre en bleu primary (#2563eb) cohérent avec template HTML
        for run in h.runs:
            run.font.color.rgb = RGBColor(0x25, 0x63, 0xEB)

    # Meta : Spécialité · Établissement · Date
    meta_parts: list[str] = []
    if options.subject:
        meta_parts.append(options.subject)
    if options.level:
        meta_parts.append(options.level)
    meta_parts.append(options.date_iso or today_iso)

    if meta_parts:
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run(" · ".join(meta_parts))

    # Espace avant disclaimer
    document.add_paragraph()


def _build_medicine_disclaimer_paragraph(document: Any) -> None:
    """Ajoute le disclaimer urgence médical EN TÊTE du body (SAFETY-CRITICAL).

    Bloc gras rouge avec numéros urgence Cameroun 117/118/119 + 112
    international. Aligné sur les standards `expert_prompts/medicine.py`
    A2 et le template HTML `medicine.html` C4.7c. JAMAIS désactivable.
    """
    from docx.shared import RGBColor

    # Ligne titre rouge gras "⚠️ AVERTISSEMENT MÉDICAL"
    p_title = document.add_paragraph()
    run_title = p_title.add_run("⚠️ AVERTISSEMENT MÉDICAL")
    run_title.bold = True
    run_title.font.color.rgb = RGBColor(0xDC, 0x26, 0x26)  # rouge #dc2626

    # Corps du disclaimer (texte informatif)
    p_body = document.add_paragraph()
    p_body.add_run(
        "Ce document est fourni à titre d'information uniquement et ne "
        "remplace en aucun cas une consultation médicale professionnelle."
    )

    # Numéros urgence en gras (visibilité maximale)
    p_numbers = document.add_paragraph()
    run_numbers = p_numbers.add_run(
        "En cas d'urgence vitale au Cameroun : 117 (Police) · 118 (Pompiers) "
        "· 119 (SAMU). À l'international : 112."
    )
    run_numbers.bold = True

    # Séparateur visuel avant body
    document.add_paragraph()


def _render_docx_sync(
    *,
    template_name: Literal["school", "minimal", "sciences", "legal", "medicine"],
    title: str | None,
    markdown_source: str,
    options: DocumentGenerateOptions,
    max_pages: int,
) -> RenderedDocx:
    """Rend un DOCX complet (sync, CPU-bound).

    Appelé dans `asyncio.to_thread` pour ne pas bloquer l'event loop.

    Args:
        template_name: Slug template (school | minimal | sciences | legal |
            medicine).
        title: Titre principal du document.
        markdown_source: Contenu source markdown brut.
        options: Options de personnalisation (subject/level/date_iso
            réutilisés sémantiquement par template — cf. docstring
            `DocumentTemplate` dans schemas.py).
        max_pages: Cap dur pages (heuristique paragraphes).

    Returns:
        RenderedDocx avec bytes + pages + truncated + size.

    Raises:
        Exception: Toute erreur python-docx (struct OOXML, OOM).
            Catch côté caller pour mapping vers DocumentRenderFailedError.
    """
    from docx import Document

    doc = Document()
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Header par template (dispatch 5 templates C4.7a + C4.7c)
    if template_name == "school":
        _build_school_header(doc, title=title, options=options, today_iso=today_iso)
    elif template_name == "sciences":
        _build_sciences_header(doc, title=title, options=options, today_iso=today_iso)
    elif template_name == "legal":
        _build_legal_header(doc, title=title, options=options, today_iso=today_iso)
    elif template_name == "medicine":
        _build_medicine_header(doc, title=title, options=options, today_iso=today_iso)
        # SAFETY-CRITICAL : disclaimer urgence EN TÊTE body (FIGÉ, jamais
        # désactivable). Aligné `expert_prompts/medicine.py` A2 +
        # `templates/medicine.html` C4.7c.
        _build_medicine_disclaimer_paragraph(doc)
    else:  # minimal
        _build_minimal_header(doc, title=title)

    # Body : parse markdown → tokens → traversal
    tokens = _MD.parse(markdown_source or "")
    max_paragraphs = max_pages * _PARAGRAPHS_PER_PAGE_ESTIMATE
    paragraphs_written, truncated = _render_tokens_to_docx(doc, tokens, max_paragraphs=max_paragraphs)

    # Note de troncature si applicable
    if truncated:
        note_p = doc.add_paragraph()
        note_run = note_p.add_run(
            f"\n[Document tronqué à environ {max_pages} pages — "
            f"scinde le contenu en plusieurs documents plus courts.]"
        )
        note_run.italic = True

    # Save en BytesIO
    output = io.BytesIO()
    doc.save(output)
    docx_bytes = output.getvalue()

    # Estimation pages (heuristique ceiling division — au moins 1 page)
    if paragraphs_written == 0:
        estimated_pages = 1
    else:
        estimated_pages = (
            paragraphs_written + _PARAGRAPHS_PER_PAGE_ESTIMATE - 1
        ) // _PARAGRAPHS_PER_PAGE_ESTIMATE

    return RenderedDocx(
        docx_bytes=docx_bytes,
        pages=min(estimated_pages, max_pages),
        truncated=truncated,
        size_bytes=len(docx_bytes),
    )


# ── Public API async ─────────────────────────────────────────────────


async def render_markdown_to_docx(
    *,
    template_name: Literal["school", "minimal", "sciences", "legal", "medicine"],
    title: str | None,
    markdown_source: str,
    options: DocumentGenerateOptions,
    timeout_seconds: float = _DEFAULT_RENDER_TIMEOUT_SECONDS,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> RenderedDocx:
    """Rend markdown → DOCX complet avec timeout + cap pages.

    Pipeline :
        1. markdown-it parse AST tokens (singleton _MD)
        2. python-docx render dans thread (sync, ~0.5-3s typique)
        3. Timeout asyncio 30s default (kill si dépasse)
        4. Cap pages estimé via heuristique paragraphes

    Args:
        template_name: Slug template (school | minimal | sciences | legal |
            medicine). Doit appartenir à ALLOWED_TEMPLATES côté Pydantic.
        title: Titre principal du document (None = défaut par template).
        markdown_source: Contenu source markdown brut.
        options: Options de personnalisation (subject/level/date_iso
            réutilisés sémantiquement par template — cf. docstring
            `DocumentTemplate` dans schemas.py).
        timeout_seconds: Timeout hardcap render (défaut 30s).
        max_pages: Cap dur pages estimé (défaut 50).

    Returns:
        RenderedDocx avec metadata complète.

    Raises:
        DocumentRenderFailedError: Sur timeout, exception python-docx,
            ou markdown source vide.
    """
    if not markdown_source or not markdown_source.strip():
        raise DocumentRenderFailedError(
            "Contenu source vide — impossible de rendre un DOCX."
        )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                _render_docx_sync,
                template_name=template_name,
                title=title,
                markdown_source=markdown_source,
                options=options,
                max_pages=max_pages,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        log.warning(
            "documents.render.docx_timeout",
            timeout_seconds=timeout_seconds,
            source_chars=len(markdown_source),
            template=template_name,
        )
        raise DocumentRenderFailedError(
            f"Rendu DOCX dépassant le timeout {timeout_seconds}s. "
            "Essaie avec un document plus court."
        ) from exc
    except Exception as exc:
        log.warning(
            "documents.render.docx_error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
            source_chars=len(markdown_source),
            template=template_name,
        )
        raise DocumentRenderFailedError(
            "Rendu DOCX impossible — erreur de génération interne. "
            "Vérifie que le document source ne contient pas de structures invalides."
        ) from exc

    log.info(
        "documents.render.docx_completed",
        pages=result.pages,
        size_bytes=result.size_bytes,
        truncated=result.truncated,
        template=template_name,
    )

    return result
