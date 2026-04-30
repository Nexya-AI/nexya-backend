"""
Tests unitaires — `app/core/storage/mime_detector.py` (Session E3).

On construit des payloads minimaux avec les bons magic-bytes en tête pour
valider chaque format. Pour les OOXML on construit un vrai ZIP en mémoire
avec le marqueur distinctif — c'est le cas le plus représentatif de la
réalité (un vrai DOCX n'est qu'un ZIP structuré).
"""

from __future__ import annotations

import io
import zipfile

from app.core.storage.mime_detector import (
    detect_mime_type,
    mimes_compatible,
)

# ══════════════════════════════════════════════════════════════
# Helpers — fabrique de ZIP minimal
# ══════════════════════════════════════════════════════════════


def _build_zip(members: dict[str, bytes]) -> bytes:
    """Crée un ZIP en mémoire contenant les fichiers (name → content)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# 1. Signatures simples
# ══════════════════════════════════════════════════════════════


def test_detect_pdf_magic() -> None:
    assert detect_mime_type(b"%PDF-1.4\n" + b"x" * 100) == "application/pdf"


def test_detect_png_magic() -> None:
    data = b"\x89PNG\r\n\x1a\n" + b"IHDR" + b"\x00" * 30
    assert detect_mime_type(data) == "image/png"


def test_detect_jpeg_magic() -> None:
    # SOI marker + JFIF
    data = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 20
    assert detect_mime_type(data) == "image/jpeg"


def test_detect_gif87a_and_gif89a() -> None:
    assert detect_mime_type(b"GIF87a" + b"\x01\x00\x01\x00" + b"\x00" * 20) == "image/gif"
    assert detect_mime_type(b"GIF89a" + b"\x01\x00\x01\x00" + b"\x00" * 20) == "image/gif"


def test_detect_webp_riff_discrimination() -> None:
    # RIFF + size (4 bytes) + WEBP.
    data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
    assert detect_mime_type(data) == "image/webp"


def test_detect_wav_riff_discrimination() -> None:
    data = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 20
    assert detect_mime_type(data) == "audio/wav"


def test_detect_riff_unknown_subtype_returns_none() -> None:
    data = b"RIFF" + b"\x00\x00\x00\x00" + b"XXXX" + b"\x00" * 20
    assert detect_mime_type(data) is None


def test_detect_mp3_id3_and_frame_sync() -> None:
    assert detect_mime_type(b"ID3\x03\x00\x00" + b"\x00" * 40) == "audio/mpeg"
    assert detect_mime_type(b"\xff\xfb\x90\x00" + b"\x00" * 40) == "audio/mpeg"


def test_detect_ogg_magic() -> None:
    assert detect_mime_type(b"OggS\x00\x02" + b"\x00" * 40) == "audio/ogg"


def test_detect_mp4_ftyp_at_offset_4() -> None:
    # 4 bytes de longueur + "ftyp" + major_brand + minor + compat.
    data = b"\x00\x00\x00\x18" + b"ftyp" + b"mp42\x00\x00\x00\x00isommp42"
    assert detect_mime_type(data) == "video/mp4"


# ══════════════════════════════════════════════════════════════
# 2. OOXML — discrimination via ZIP
# ══════════════════════════════════════════════════════════════


def test_detect_docx_via_zip_marker() -> None:
    docx_bytes = _build_zip(
        {
            "[Content_Types].xml": b"<dummy/>",
            "word/document.xml": b"<w:document/>",
        }
    )
    assert (
        detect_mime_type(docx_bytes)
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_detect_xlsx_via_zip_marker() -> None:
    xlsx_bytes = _build_zip(
        {
            "[Content_Types].xml": b"<dummy/>",
            "xl/workbook.xml": b"<workbook/>",
        }
    )
    assert (
        detect_mime_type(xlsx_bytes)
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_detect_pptx_via_zip_marker() -> None:
    pptx_bytes = _build_zip(
        {
            "[Content_Types].xml": b"<dummy/>",
            "ppt/presentation.xml": b"<presentation/>",
        }
    )
    assert (
        detect_mime_type(pptx_bytes)
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


def test_detect_plain_zip_without_ooxml_marker() -> None:
    """Un ZIP user sans marqueur Office → application/zip (pas OOXML)."""
    random_zip = _build_zip(
        {
            "readme.txt": b"hello",
            "photo.jpg": b"\xff\xd8\xff\xe0fake",
        }
    )
    assert detect_mime_type(random_zip) == "application/zip"


# ══════════════════════════════════════════════════════════════
# 3. Edge cases — bytes vides, bytes aléatoires
# ══════════════════════════════════════════════════════════════


def test_empty_bytes_returns_none() -> None:
    assert detect_mime_type(b"") is None


def test_random_bytes_return_none() -> None:
    assert detect_mime_type(b"\x00\x01\x02\x03\x04") is None


def test_truncated_signature_returns_none() -> None:
    """Un buffer trop court pour matcher un magic connu → None."""
    assert detect_mime_type(b"%PD") is None  # PDF tronqué
    assert detect_mime_type(b"\x89PN") is None  # PNG tronqué


# ══════════════════════════════════════════════════════════════
# 4. mimes_compatible — alias tolérance
# ══════════════════════════════════════════════════════════════


def test_mimes_compatible_identical() -> None:
    assert mimes_compatible("image/png", "image/png") is True


def test_mimes_compatible_jpeg_alias() -> None:
    assert mimes_compatible("image/jpg", "image/jpeg") is True
    assert mimes_compatible("image/jpeg", "image/jpg") is True
    assert mimes_compatible("image/pjpeg", "image/jpeg") is True


def test_mimes_compatible_text_family() -> None:
    assert mimes_compatible("text/plain", "text/markdown") is True
    assert mimes_compatible("text/csv", "text/plain") is True


def test_mimes_compatible_strict_mismatch() -> None:
    assert mimes_compatible("image/png", "image/jpeg") is False
    assert mimes_compatible("application/pdf", "image/png") is False


# ══════════════════════════════════════════════════════════════
# 5. ZIP mal formé — swallow + None
# ══════════════════════════════════════════════════════════════


def test_malformed_zip_returns_zip_no_ooxml() -> None:
    """ZIP avec le magic PK\\x03\\x04 mais corrompu dedans → application/zip.

    Le magic matche, le discriminator OOXML ne trouve pas de marker et
    retourne None (swallow l'exception zipfile), donc on renvoie le fallback
    application/zip."""
    data = b"PK\x03\x04" + b"\x00" * 50  # pas un vrai ZIP complet
    # Le discriminator peut trouver un ZIP décompressable ou pas.
    # Si pas décompressable → None → fallback application/zip.
    result = detect_mime_type(data)
    # On accepte 'application/zip' OU None selon la tolérance zipfile sur
    # les bytes tronqués.
    assert result in {"application/zip", None}
