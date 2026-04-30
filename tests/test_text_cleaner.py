"""
Tests unitaires — `app.features.files.text_cleaner.clean_extracted_text`.

Ne dépend d'aucun SDK externe ni de DB. Vérifie les 6 passes du pipeline
de pré-nettoyage D4 + la préservation des marqueurs `[[PAGE:N]]`.
"""

from __future__ import annotations

import unicodedata

from app.features.files.text_cleaner import clean_extracted_text

# ── 1. Normalisation NFC ──────────────────────────────────────────


def test_cleaner_nfc_normalizes_accents() -> None:
    # `e` + U+0301 (accent aigu combinant) doit être fusionné en `é` NFC.
    decomposed = "développé"
    expected_nfc = unicodedata.normalize("NFC", decomposed)
    result = clean_extracted_text(decomposed)
    assert result == expected_nfc
    assert result == "développé"


# ── 2. Collapse whitespace horizontal ─────────────────────────────


def test_cleaner_collapses_internal_whitespace() -> None:
    raw = "Ivan    est\t\tdev     Flutter"
    result = clean_extracted_text(raw)
    assert result == "Ivan est dev Flutter"


# ── 3. Collapse ≥ 3 sauts de ligne → 2 ────────────────────────────


def test_cleaner_collapses_multiple_newlines_to_two() -> None:
    raw = "paragraphe 1\n\n\n\n\nparagraphe 2"
    result = clean_extracted_text(raw)
    assert result == "paragraphe 1\n\nparagraphe 2"


# ── 4. Déhyphénation fin de ligne ────────────────────────────────


def test_cleaner_dehyphenates_line_breaks() -> None:
    raw = "déve-\nloppement appro-\nximatif"
    result = clean_extracted_text(raw)
    assert result == "développement approximatif"


# ── 5. Strip headers/footers ─────────────────────────────────────


def test_cleaner_strips_page_headers_footers() -> None:
    raw = (
        "Introduction au document.\n"
        "3 / 10\n"
        "Le premier chapitre explique...\n"
        "Page 4\n"
        "Le deuxième chapitre..."
    )
    result = clean_extracted_text(raw)
    assert "3 / 10" not in result
    assert "Page 4" not in result
    assert "Introduction au document." in result
    assert "Le premier chapitre explique..." in result
    assert "Le deuxième chapitre..." in result


# ── 6. Préservation des marqueurs [[PAGE:N]] ──────────────────────


def test_cleaner_preserves_page_markers() -> None:
    raw = "[[PAGE:1]]\nContenu page 1.\n[[PAGE:2]]\nContenu page 2."
    result = clean_extracted_text(raw)
    assert "[[PAGE:1]]" in result
    assert "[[PAGE:2]]" in result


# ── 7. Edge cases ─────────────────────────────────────────────────


def test_cleaner_handles_empty_and_whitespace_safely() -> None:
    assert clean_extracted_text("") == ""
    assert clean_extracted_text("   \n\n   \t  ") == ""
