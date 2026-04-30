"""
Tests unitaires — `app/core/storage/text_extractor.py` (Session E3).

Trois chemins :
- PDF via `pypdf` (on construit un vrai PDF minimal en mémoire avec pypdf).
- DOCX via `zipfile` + `xml.etree` (on construit un DOCX minimal en mémoire).
- Plain text (UTF-8 + fallback latin-1).

Les PDFs et DOCX construits dans les tests sont **minimalistes mais
valides**, pas des stubs bytes arbitraires — on valide vraiment le pipeline
d'extraction.
"""

from __future__ import annotations

import io
import zipfile

from app.core.storage.text_extractor import extract_text

# ══════════════════════════════════════════════════════════════
# Helpers — fabrique de PDF et DOCX minimaux
# ══════════════════════════════════════════════════════════════


def _build_minimal_pdf(text_per_page: list[str]) -> bytes:
    """Construit un PDF avec N pages, une ligne de texte par page via pypdf."""

    # Approche pragmatique : pypdf est lourd pour construire un PDF from
    # scratch, on utilise l'approche "decode then encode" : on génère un
    # PDF valide très minimal manuellement. C'est fragile mais suffisant
    # pour tester le chemin happy `extract_text` qui lit des pages vides
    # avec `page.extract_text() or ""`.
    #
    # Pour avoir du texte réellement extractable, on utilise le trick du
    # PDF le plus simple qui contient un Tj stream.
    parts = []
    # Header
    parts.append(b"%PDF-1.4\n")
    pages_kids = []
    objects = []

    # On va créer 3 objets par page : Page, Contents stream, Font.
    # Layout : 1 Catalog, 1 Pages, puis N×Page+Contents, puis 1 Font.
    # IDs :
    #   1: Catalog
    #   2: Pages
    #   3..(2+2N): Page1/Contents1/Page2/Contents2/...
    #   (3+2N): Font
    n = len(text_per_page)
    font_id = 3 + 2 * n

    # Catalog
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    # Pages (kids = [3, 5, 7, ...])
    kids_str = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(f"2 0 obj\n<< /Type /Pages /Count {n} /Kids [{kids_str}] >>\nendobj\n".encode())
    for i, text in enumerate(text_per_page):
        page_id = 3 + 2 * i
        contents_id = page_id + 1
        page_obj = (
            f"{page_id} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 612 792] /Contents {contents_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>\nendobj\n"
        ).encode()
        # Stream : BT /F1 12 Tf 72 720 Td (<text>) Tj ET
        # On échappe les ( ) dans le texte.
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = (f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET\n").encode()
        contents_obj = (
            f"{contents_id} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"endstream\nendobj\n"
        )
        objects.append(page_obj)
        objects.append(contents_obj)

    objects.append(
        f"{font_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n".encode()
    )

    # Build body + xref
    body = b"".join(parts + objects)
    # xref (simplifié — positions relatives)
    # On calcule les offsets des objets depuis le début.
    header_len = len(parts[0])
    offsets = [header_len]
    for obj in objects[:-1]:
        offsets.append(offsets[-1] + len(obj))

    xref_pos = len(body)
    xref_lines = [b"xref\n", f"0 {font_id + 1}\n".encode(), b"0000000000 65535 f \n"]
    for off in offsets:
        xref_lines.append(f"{off:010d} 00000 n \n".encode())
    trailer = (
        f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )

    return body + b"".join(xref_lines) + trailer


def _build_minimal_docx(paragraphs: list[str]) -> bytes:
    """Construit un .docx minimal avec N paragraphes de texte."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    p_blocks = []
    for text in paragraphs:
        # Escape minimal XML.
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        p_blocks.append(
            f'<w:p xmlns:w="{ns}"><w:r><w:t xml:space="preserve">{safe}</w:t></w:r></w:p>'
        )
    document_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>'
        f"{''.join(p_blocks)}"
        f"</w:body></w:document>"
    ).encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"<types/>")
        zf.writestr("word/document.xml", document_xml)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# 1. PDF
# ══════════════════════════════════════════════════════════════


def test_extract_pdf_simple_one_page() -> None:
    pdf = _build_minimal_pdf(["Hello world from NEXYA"])
    result = extract_text(pdf, "application/pdf")
    assert result.status == "ok"
    assert result.page_count == 1
    assert "Hello" in result.text or "hello" in result.text.lower()
    assert result.truncated is False


def test_extract_pdf_multi_page() -> None:
    pdf = _build_minimal_pdf(["Page un", "Page deux", "Page trois"])
    result = extract_text(pdf, "application/pdf")
    assert result.status == "ok"
    assert result.page_count == 3


def test_extract_pdf_truncated() -> None:
    # 50 pages × 100 chars = 5000 chars > max_chars=1000 → truncated.
    pdf = _build_minimal_pdf([f"page {i} " + "x" * 100 for i in range(50)])
    result = extract_text(pdf, "application/pdf", max_chars=1000)
    assert result.status == "ok"
    assert result.truncated is True
    assert len(result.text) <= 1000


def test_extract_pdf_corrupted() -> None:
    result = extract_text(b"%PDF-1.4\nGARBAGE", "application/pdf")
    assert result.status == "failed"
    assert result.text == ""


# ══════════════════════════════════════════════════════════════
# 2. DOCX
# ══════════════════════════════════════════════════════════════


def test_extract_docx_with_paragraphs() -> None:
    docx = _build_minimal_docx(
        [
            "Premier paragraphe.",
            "Deuxième paragraphe avec accents éàç.",
        ]
    )
    result = extract_text(
        docx,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.status == "ok"
    assert result.page_count is None  # DOCX n'a pas de notion de page.
    assert "Premier paragraphe." in result.text
    assert "éàç" in result.text


def test_extract_docx_without_document_xml() -> None:
    """Un ZIP qui n'a pas word/document.xml → status='failed'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", b"not a docx")
    result = extract_text(
        buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.status == "failed"


def test_extract_docx_truncated() -> None:
    docx = _build_minimal_docx(["x" * 300] * 10)  # 3000 chars total.
    result = extract_text(
        docx,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        max_chars=500,
    )
    assert result.status == "ok"
    assert result.truncated is True
    assert len(result.text) <= 500


# ══════════════════════════════════════════════════════════════
# 3. Plain text
# ══════════════════════════════════════════════════════════════


def test_extract_plain_utf8() -> None:
    data = "Bonjour éàç".encode()
    result = extract_text(data, "text/plain")
    assert result.status == "ok"
    assert result.text == "Bonjour éàç"


def test_extract_plain_latin1_fallback() -> None:
    """Un fichier en latin-1 (Windows ancien) doit passer via fallback."""
    data = "Résumé".encode("latin-1")
    result = extract_text(data, "text/plain")
    # Status 'ok' car on ne perd pas de bytes (latin-1 accepte tout).
    assert result.status == "ok"
    assert len(result.text) > 0


def test_extract_markdown_treated_as_plain() -> None:
    data = b"# Titre\n\nParagraphe **gras**."
    result = extract_text(data, "text/markdown")
    assert result.status == "ok"
    assert "Titre" in result.text


# ══════════════════════════════════════════════════════════════
# 4. Unsupported
# ══════════════════════════════════════════════════════════════


def test_extract_unsupported_mime() -> None:
    result = extract_text(b"random bytes", "application/octet-stream")
    assert result.status == "unsupported"
    assert result.text == ""
    assert result.page_count is None


def test_extract_image_unsupported() -> None:
    result = extract_text(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "image/png")
    assert result.status == "unsupported"


# ══════════════════════════════════════════════════════════════
# 5. Empty content
# ══════════════════════════════════════════════════════════════


def test_extract_empty_text_plain() -> None:
    result = extract_text(b"", "text/plain")
    assert result.status == "empty"


def test_extract_whitespace_only_text_plain() -> None:
    result = extract_text(b"   \n\t  ", "text/plain")
    assert result.status == "empty"
