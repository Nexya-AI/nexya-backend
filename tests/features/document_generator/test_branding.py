"""Tests unitaires C4.8 + C4.9 — branding NEXYA + conformité AI Act.

Couverture exhaustive du module `app.features.document_generator.branding` :
- BrandingContext dataclass + frozen + idempotence
- generate_intelligent_filename (slugify NFKD, cas limites)
- build_pdf_branding_header_css / build_pdf_branding_footer_css
- build_invisible_html_marker
- apply_pdf_native_metadata (avec pikepdf real)
- apply_docx_branding_header / enrich_docx_footer_with_branding
- apply_docx_core_properties / apply_docx_invisible_marker (avec python-docx real)
- Fail-safe absolu sur tous les helpers (exception → False sans raise)

Mock-first strict : aucun appel réseau, aucune lecture disque (sauf
pour pikepdf/python-docx in-memory).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

from app.features.document_generator.branding import (
    AI_TRANSPARENCY_NOTICE_EN,
    AI_TRANSPARENCY_NOTICE_FR,
    BRAND_COMPANY,
    BRAND_NAME,
    BRANDING_VERSION,
    _slugify,
    apply_docx_branding_header,
    apply_docx_core_properties,
    apply_docx_invisible_marker,
    apply_pdf_native_metadata,
    build_branding_context,
    build_invisible_html_marker,
    build_pdf_branding_footer_css,
    build_pdf_branding_header_css,
    enrich_docx_footer_with_branding,
    generate_intelligent_filename,
)

# ══════════════════════════════════════════════════════════════
# 1. Constantes brand exposées
# ══════════════════════════════════════════════════════════════


def test_branding_version_format() -> None:
    """`BRANDING_VERSION` doit suivre format `cXX-vY` (C4.8 = c48-v1)."""
    assert BRANDING_VERSION == "c48-v1"


def test_brand_constants_non_empty() -> None:
    """Constantes brand doivent être non-vides et exactes."""
    assert BRAND_NAME == "NEXYA AI"
    assert BRAND_COMPANY == "Nexyalabs"


def test_ai_transparency_notices_mention_ai_act() -> None:
    """Le disclaimer AI Act doit explicitement mentionner « AI Act »."""
    assert "AI Act" in AI_TRANSPARENCY_NOTICE_FR
    assert "AI Act" in AI_TRANSPARENCY_NOTICE_EN
    assert "2024/1689" in AI_TRANSPARENCY_NOTICE_FR
    assert "2024/1689" in AI_TRANSPARENCY_NOTICE_EN
    # FR doit mentionner « intelligence artificielle » en français
    assert "intelligence artificielle" in AI_TRANSPARENCY_NOTICE_FR.lower()


# ══════════════════════════════════════════════════════════════
# 2. BrandingContext dataclass + build_branding_context
# ══════════════════════════════════════════════════════════════


def test_branding_context_is_frozen() -> None:
    """`BrandingContext` doit être frozen (immutable post-construction)."""
    ctx = build_branding_context(template="minimal", title="Test")
    with pytest.raises(Exception):  # FrozenInstanceError
        ctx.title = "Tampered"  # type: ignore[misc]


def test_build_branding_context_fr_default() -> None:
    """`build_branding_context` défaut locale=fr."""
    fixed_now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="school", title="Devoir Maths", now=fixed_now)
    assert ctx.version == BRANDING_VERSION
    assert ctx.brand_name == BRAND_NAME
    assert ctx.brand_company == BRAND_COMPANY
    assert ctx.template == "school"
    assert ctx.title == "Devoir Maths"
    assert ctx.locale == "fr"
    assert ctx.ai_notice == AI_TRANSPARENCY_NOTICE_FR
    assert ctx.generated_at == fixed_now
    assert ctx.date_iso == "2026-05-31"


def test_build_branding_context_en_locale() -> None:
    """`build_branding_context(locale='en')` utilise le notice EN."""
    ctx = build_branding_context(template="minimal", title="Doc", locale="en")
    assert ctx.locale == "en"
    assert ctx.ai_notice == AI_TRANSPARENCY_NOTICE_EN


def test_build_branding_context_no_title() -> None:
    """Title=None accepté (template default sera utilisé downstream)."""
    ctx = build_branding_context(template="medicine", title=None)
    assert ctx.title is None


def test_build_branding_context_now_uses_utc_when_omitted() -> None:
    """`now=None` → datetime.now(UTC) utilisé (date_iso non-vide)."""
    ctx = build_branding_context(template="minimal", title="X")
    # date_iso format YYYY-MM-DD, year ≥ 2026 (sanity check)
    assert len(ctx.date_iso) == 10
    assert ctx.date_iso[4] == "-"
    assert ctx.date_iso[7] == "-"


# ══════════════════════════════════════════════════════════════
# 3. _slugify (helper interne testé via filename intelligent)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected_substr",
    [
        ("Bonjour le monde", "bonjour-le-monde"),
        ("Café à Paris", "cafe-a-paris"),  # NFKD strip accents
        ("Test/path\\with*special?chars", "test-path-with-special-chars"),
        ("   leading-trailing   ", "leading-trailing"),
        ("HÉLLO WÖRLD", "hello-world"),
        ("ndolé sans arachides", "ndole-sans-arachides"),
        ("école française", "ecole-francaise"),
    ],
)
def test_slugify_handles_unicode_and_special_chars(raw: str, expected_substr: str) -> None:
    """`_slugify` doit normaliser NFKD + lowercase + ASCII-only."""
    result = _slugify(raw)
    assert result == expected_substr


def test_slugify_empty_returns_document_fallback() -> None:
    """`_slugify('')` retourne `document` (jamais vide)."""
    assert _slugify("") == "document"
    assert _slugify("   ") == "document"
    assert _slugify("***") == "document"  # tout strip


def test_slugify_max_len_truncates() -> None:
    """`_slugify` respecte max_len."""
    long = "a" * 200
    result = _slugify(long, max_len=50)
    assert len(result) == 50


# ══════════════════════════════════════════════════════════════
# 4. generate_intelligent_filename
# ══════════════════════════════════════════════════════════════


def test_generate_intelligent_filename_pdf_with_title() -> None:
    """Filename format `nexya_<template>_<slug>_<date>.pdf`."""
    fixed_now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="minimal", title="Cours photosynthèse", now=fixed_now)
    filename = generate_intelligent_filename(ctx, extension="pdf")
    assert filename == "nexya_minimal_cours-photosynthese_2026-05-31.pdf"


def test_generate_intelligent_filename_docx_school_template() -> None:
    """Filename DOCX cohérent avec template school."""
    fixed_now = datetime(2026, 5, 31, 0, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="school", title="Devoir Math", now=fixed_now)
    filename = generate_intelligent_filename(ctx, extension="docx")
    assert filename == "nexya_school_devoir-math_2026-05-31.docx"


def test_generate_intelligent_filename_no_title_uses_fallback() -> None:
    """Title=None → fallback `document` dans le slug."""
    fixed_now = datetime(2026, 5, 31, 0, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="minimal", title=None, now=fixed_now)
    filename = generate_intelligent_filename(ctx, extension="pdf")
    assert filename == "nexya_minimal_document_2026-05-31.pdf"


def test_generate_intelligent_filename_strips_accents() -> None:
    """Slug ASCII-safe (no accents, no special chars)."""
    fixed_now = datetime(2026, 5, 31, 0, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="medicine", title="Diabète Type 2", now=fixed_now)
    filename = generate_intelligent_filename(ctx, extension="pdf")
    # NFKD strip accents : « Diabète » → « diabete »
    assert "diabete" in filename
    assert "è" not in filename
    assert filename == "nexya_medicine_diabete-type-2_2026-05-31.pdf"


# ══════════════════════════════════════════════════════════════
# 5. CSS builders (PDF)
# ══════════════════════════════════════════════════════════════


def test_build_pdf_branding_header_css_contains_brand_name() -> None:
    """Header CSS doit injecter `[NEXYA AI]` dans @top-left."""
    ctx = build_branding_context(template="minimal", title="X")
    css = build_pdf_branding_header_css(ctx)
    assert "@page" in css
    assert "@top-left" in css
    assert f"[{BRAND_NAME}]" in css
    assert "italic" in css


def test_build_pdf_branding_footer_css_contains_brand_and_date() -> None:
    """Footer CSS doit contenir brand_name + company + date_iso."""
    fixed_now = datetime(2026, 5, 31, 0, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="minimal", title="X", now=fixed_now)
    css = build_pdf_branding_footer_css(ctx)
    assert "@bottom-center" in css
    assert BRAND_NAME in css
    assert BRAND_COMPANY in css
    assert "2026-05-31" in css
    assert "Généré par" in css


def test_build_pdf_branding_footer_css_en_locale() -> None:
    """Footer EN utilise « Generated by » au lieu de « Généré par »."""
    ctx = build_branding_context(template="minimal", title="X", locale="en")
    css = build_pdf_branding_footer_css(ctx)
    assert "Generated by" in css
    assert "Généré par" not in css


# ══════════════════════════════════════════════════════════════
# 6. Invisible HTML marker
# ══════════════════════════════════════════════════════════════


def test_build_invisible_html_marker_format() -> None:
    """Marker = `<!-- NEXYA-DOC-BRANDING version=... template=... -->`."""
    fixed_now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="legal", title="Bail", now=fixed_now)
    marker = build_invisible_html_marker(ctx)
    assert marker.startswith("<!--")
    assert marker.endswith("-->")
    assert "NEXYA-DOC-BRANDING" in marker
    assert "version=c48-v1" in marker
    assert "template=legal" in marker
    assert "2026-05-31" in marker  # generated_at ISO


def test_invisible_marker_is_grep_able() -> None:
    """Le marker doit être facilement grep-able sur un PDF source."""
    ctx = build_branding_context(template="sciences", title="Thèse")
    marker = build_invisible_html_marker(ctx)
    # « strings file.pdf | grep NEXYA-DOC » doit matcher
    assert "NEXYA-DOC" in marker


# ══════════════════════════════════════════════════════════════
# 7. apply_pdf_native_metadata (avec pikepdf real)
# ══════════════════════════════════════════════════════════════


def _make_minimal_pdf_bytes() -> bytes:
    """Construit un PDF minimal valide via pikepdf (pour tests metadata)."""
    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(595, 842))  # A4
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def test_apply_pdf_native_metadata_sets_info_dict() -> None:
    """`apply_pdf_native_metadata` pose /Title /Author /Subject etc."""
    import pikepdf

    pdf_bytes = _make_minimal_pdf_bytes()
    ctx = build_branding_context(template="minimal", title="Test")

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        result = apply_pdf_native_metadata(pdf, ctx)
        assert result is True
        assert str(pdf.docinfo["/Author"]) == BRAND_NAME
        assert str(pdf.docinfo["/Title"]) == "Test"
        assert "NEXYA" in str(pdf.docinfo["/Producer"])
        assert "NEXYA" in str(pdf.docinfo["/Creator"])


def test_apply_pdf_native_metadata_includes_ai_notice_in_subject() -> None:
    """Le /Subject doit contenir le disclaimer AI Act."""
    import pikepdf

    pdf_bytes = _make_minimal_pdf_bytes()
    ctx = build_branding_context(template="medicine", title=None)

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        apply_pdf_native_metadata(pdf, ctx)
        subject = str(pdf.docinfo["/Subject"])
        assert "AI Act" in subject or "intelligence artificielle" in subject.lower()


def test_apply_pdf_native_metadata_fail_safe_on_exception() -> None:
    """Si pikepdf.docinfo throw, fail-safe retourne False sans raise."""

    # Mock object qui throw sur __setitem__
    class _ThrowingDocInfo:
        def __setitem__(self, key, value):
            raise RuntimeError("Simulated docinfo failure")

    class _ThrowingPdf:
        docinfo = _ThrowingDocInfo()

        def open_metadata(self):
            raise RuntimeError("should not be reached")

    ctx = build_branding_context(template="minimal", title="X")
    result = apply_pdf_native_metadata(_ThrowingPdf(), ctx)
    assert result is False  # fail-safe absolu


# ══════════════════════════════════════════════════════════════
# 8. apply_docx_branding_header (avec python-docx real)
# ══════════════════════════════════════════════════════════════


def test_apply_docx_branding_header_adds_run_in_header() -> None:
    """`[NEXYA AI]` ajouté au header de la première section."""
    from docx import Document

    doc = Document()
    ctx = build_branding_context(template="minimal", title="Test")

    result = apply_docx_branding_header(doc, ctx)
    assert result is True

    # Vérifie qu'un run avec [NEXYA AI] existe dans le header
    section = doc.sections[0]
    header_paragraphs = section.header.paragraphs
    all_text = " ".join(p.text for p in header_paragraphs)
    assert f"[{BRAND_NAME}]" in all_text


def test_apply_docx_branding_header_fail_safe_on_none_document() -> None:
    """Document None ou bad → False sans raise."""
    ctx = build_branding_context(template="minimal", title="X")

    class _BadDoc:
        sections: list = []  # provoque IndexError

    result = apply_docx_branding_header(_BadDoc(), ctx)
    assert result is False


# ══════════════════════════════════════════════════════════════
# 9. enrich_docx_footer_with_branding (page counter)
# ══════════════════════════════════════════════════════════════


def test_enrich_docx_footer_adds_page_counter_field() -> None:
    """Footer enrichi avec `<w:fldSimple w:instr="PAGE"/>` natif."""
    from docx import Document

    doc = Document()
    ctx = build_branding_context(template="minimal", title="X")

    result = enrich_docx_footer_with_branding(doc, ctx)
    assert result is True

    # Le footer doit contenir au moins 2 nouveaux paragraphes
    footer = doc.sections[0].footer
    paragraph_count = len(footer.paragraphs)
    assert paragraph_count >= 2  # paragraph 0 vide initial + ajouts

    # Cherche fldSimple PAGE / NUMPAGES dans le XML du footer
    from lxml import etree

    footer_xml = etree.tostring(footer._element, pretty_print=True).decode()
    assert 'w:instr="PAGE"' in footer_xml
    assert 'w:instr="NUMPAGES"' in footer_xml


def test_enrich_docx_footer_includes_company_and_date() -> None:
    """Le footer doit contenir Nexyalabs + date_iso."""
    from docx import Document

    doc = Document()
    fixed_now = datetime(2026, 5, 31, 0, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="minimal", title="X", now=fixed_now)

    enrich_docx_footer_with_branding(doc, ctx)

    footer = doc.sections[0].footer
    all_text = " ".join(p.text for p in footer.paragraphs)
    assert BRAND_COMPANY in all_text
    assert "2026-05-31" in all_text


# ══════════════════════════════════════════════════════════════
# 10. apply_docx_core_properties (C4.9)
# ══════════════════════════════════════════════════════════════


def test_apply_docx_core_properties_sets_all_fields() -> None:
    """`core_properties` doivent contenir author, title, comments etc."""
    from docx import Document

    doc = Document()
    fixed_now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)
    ctx = build_branding_context(template="legal", title="Contrat OHADA", now=fixed_now)

    result = apply_docx_core_properties(doc, ctx)
    assert result is True

    cp = doc.core_properties
    assert cp.title == "Contrat OHADA"
    assert cp.author == BRAND_NAME
    assert cp.last_modified_by == BRAND_NAME
    assert "AI Act" in cp.comments
    assert cp.category == "legal"
    assert "NEXYA" in cp.keywords


def test_apply_docx_core_properties_uses_template_default_title_if_none() -> None:
    """Title=None → fallback `Document {template}` dans core_properties.title."""
    from docx import Document

    doc = Document()
    ctx = build_branding_context(template="medicine", title=None)

    apply_docx_core_properties(doc, ctx)
    cp = doc.core_properties
    assert cp.title == "Document medicine"


def test_apply_docx_core_properties_en_locale_subject() -> None:
    """Locale=en → subject `AI-generated document`."""
    from docx import Document

    doc = Document()
    ctx = build_branding_context(template="minimal", title="X", locale="en")

    apply_docx_core_properties(doc, ctx)
    cp = doc.core_properties
    assert cp.subject == "AI-generated document"


# ══════════════════════════════════════════════════════════════
# 11. apply_docx_invisible_marker
# ══════════════════════════════════════════════════════════════


def test_apply_docx_invisible_marker_adds_grep_able_paragraph() -> None:
    """Marker DOCX ajouté en fin de doc avec NEXYA-DOC-BRANDING."""
    from docx import Document

    doc = Document()
    ctx = build_branding_context(template="sciences", title="Thèse")

    result = apply_docx_invisible_marker(doc, ctx)
    assert result is True

    # Le marker doit être présent dans le texte du doc (mais invisible
    # au rendu car font 1pt blanc sur blanc)
    full_text = " ".join(p.text for p in doc.paragraphs)
    assert "NEXYA-DOC-BRANDING" in full_text
    assert "c48-v1" in full_text
    assert "template=sciences" in full_text


# ══════════════════════════════════════════════════════════════
# 12. Intégration end-to-end : tous les helpers cohabitent
# ══════════════════════════════════════════════════════════════


def test_full_docx_branding_pipeline_all_helpers_succeed() -> None:
    """Pipeline complet DOCX : header + footer + core_props + marker."""
    from docx import Document

    doc = Document()
    ctx = build_branding_context(template="minimal", title="Test Pipeline Complet")

    # Ordre identique à _render_docx_sync
    header_ok = apply_docx_branding_header(doc, ctx)
    footer_ok = enrich_docx_footer_with_branding(doc, ctx)
    core_ok = apply_docx_core_properties(doc, ctx)
    marker_ok = apply_docx_invisible_marker(doc, ctx)

    assert header_ok is True
    assert footer_ok is True
    assert core_ok is True
    assert marker_ok is True

    # Le doc doit pouvoir être saved sans erreur
    buf = io.BytesIO()
    doc.save(buf)
    assert buf.tell() > 0  # bytes écrits

    # Et re-ouvrable depuis les bytes
    buf.seek(0)
    doc2 = Document(buf)
    cp = doc2.core_properties
    assert cp.author == BRAND_NAME


def test_full_pdf_branding_pipeline_metadata_persists_after_save() -> None:
    """Pipeline PDF : metadata doit persister après save + re-load."""
    import pikepdf

    pdf_bytes = _make_minimal_pdf_bytes()
    ctx = build_branding_context(template="minimal", title="Persist Test")

    # Apply metadata + save
    buf = io.BytesIO()
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        apply_pdf_native_metadata(pdf, ctx)
        pdf.save(buf)

    # Re-load + vérif metadata
    buf.seek(0)
    with pikepdf.open(buf) as pdf_reloaded:
        assert str(pdf_reloaded.docinfo["/Title"]) == "Persist Test"
        assert str(pdf_reloaded.docinfo["/Author"]) == BRAND_NAME
        assert "AI Act" in str(pdf_reloaded.docinfo["/Subject"])
