"""Tests — bloc `document` des résultats de tâche (LOT B3).

Couvre `_build_document_result` (reconstruction tolérante depuis `metadata_json`
avec régénération du chemin de téléchargement) et `serialize_result` (enrichi
du bloc document). Pur, déterministe, sans DB ni I/O.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.features.planner.schemas import (
    TaskResultResponse,
    _build_document_result,
    serialize_result,
)


def _make_result(*, metadata_json) -> SimpleNamespace:
    """Fake `ScheduledTaskResult` ORM minimal pour `serialize_result`.

    `TaskResultResponse.model_validate(...)` lit les attributs via
    `from_attributes=True` → un SimpleNamespace suffit (pattern proven router).
    """
    return SimpleNamespace(
        id=42,
        task_id=uuid.uuid4(),
        ran_at=datetime(2026, 6, 23, 12, 0, tzinfo=UTC),
        duration_ms=1234,
        status="success",
        result_text="# Doc\n\nContenu.",
        error_text=None,
        tokens_input=100,
        tokens_output=200,
        cost_usd=Decimal("0.0001"),
        model="gemini-2.5-flash",
        provider="gemini",
        metadata_json=metadata_json,
    )


# ══════════════════════════════════════════════════════════════
# _build_document_result — happy path
# ══════════════════════════════════════════════════════════════


def test_build_document_result_happy() -> None:
    lib_id = uuid.uuid4()
    meta = {
        "output_kind": "document",
        "document": {
            "library_id": str(lib_id),
            "filename": "nexya_minimal_doc_2026-06-23.pdf",
            "format": "pdf",
            "pages": 3,
            "size_bytes": 51234,
            "truncated": False,
        },
    }
    doc = _build_document_result(meta)
    assert doc is not None
    assert doc.library_id == lib_id
    assert doc.filename == "nexya_minimal_doc_2026-06-23.pdf"
    assert doc.format == "pdf"
    assert doc.pages == 3
    assert doc.size_bytes == 51234
    assert doc.truncated is False
    # download_url RÉGÉNÉRÉE = chemin relatif authentifié (jamais persistée).
    assert doc.download_url == f"/generate/document/download/{lib_id}"


def test_build_document_result_docx_format_preserved() -> None:
    lib_id = uuid.uuid4()
    meta = {"document": {"library_id": str(lib_id), "format": "docx"}}
    doc = _build_document_result(meta)
    assert doc is not None
    assert doc.format == "docx"


# ══════════════════════════════════════════════════════════════
# _build_document_result — tolérance à la corruption → None
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "meta",
    [
        None,
        "not-a-dict",
        42,
        {},
        {"output_kind": "document"},  # pas de clé document
        {"document": "not-a-dict"},
        {"document": 123},
        {"document": {}},  # pas de library_id
        {"document": {"library_id": ""}},  # falsy
        {"document": {"library_id": None}},  # falsy
        {"document": {"library_id": "not-a-uuid"}},  # uuid invalide
        {"document": {"library_id": 42}},  # str(42) → uuid invalide
    ],
)
def test_build_document_result_none_on_corruption(meta) -> None:
    assert _build_document_result(meta) is None


def test_build_document_result_format_defaults_to_pdf_on_invalid() -> None:
    lib_id = uuid.uuid4()
    meta = {"document": {"library_id": str(lib_id), "format": "xlsx"}}
    doc = _build_document_result(meta)
    assert doc is not None
    assert doc.format == "pdf"  # défaut défensif sur métadonnée corrompue


def test_build_document_result_filename_fallback() -> None:
    lib_id = uuid.uuid4()
    meta = {"document": {"library_id": str(lib_id)}}  # pas de filename
    doc = _build_document_result(meta)
    assert doc is not None
    assert doc.filename == "document.pdf"


def test_build_document_result_excludes_bool_from_numeric_fields() -> None:
    # `bool` est sous-classe de `int` : `pages=True` ne doit PAS passer pour 1.
    lib_id = uuid.uuid4()
    meta = {
        "document": {
            "library_id": str(lib_id),
            "pages": True,
            "size_bytes": False,
            "truncated": True,
        }
    }
    doc = _build_document_result(meta)
    assert doc is not None
    assert doc.pages is None
    assert doc.size_bytes is None
    assert doc.truncated is True


def test_build_document_result_missing_optional_numerics() -> None:
    lib_id = uuid.uuid4()
    meta = {"document": {"library_id": str(lib_id), "filename": "x.pdf", "format": "pdf"}}
    doc = _build_document_result(meta)
    assert doc is not None
    assert doc.pages is None
    assert doc.size_bytes is None
    assert doc.truncated is None


# ══════════════════════════════════════════════════════════════
# serialize_result — output_kind + document combinés
# ══════════════════════════════════════════════════════════════


def test_serialize_result_generation_no_document() -> None:
    out = serialize_result(_make_result(metadata_json={"output_kind": "generation"}))
    assert isinstance(out, TaskResultResponse)
    assert out.output_kind == "generation"
    assert out.document is None


def test_serialize_result_reminder_no_document() -> None:
    out = serialize_result(_make_result(metadata_json={"output_kind": "reminder"}))
    assert out.output_kind == "reminder"
    assert out.document is None


def test_serialize_result_document_block_populated() -> None:
    lib_id = uuid.uuid4()
    out = serialize_result(
        _make_result(
            metadata_json={
                "output_kind": "document",
                "document": {
                    "library_id": str(lib_id),
                    "filename": "rapport.pdf",
                    "format": "pdf",
                    "pages": 2,
                    "size_bytes": 12000,
                    "truncated": False,
                },
            }
        )
    )
    assert out.output_kind == "document"
    assert out.document is not None
    assert out.document.library_id == lib_id
    assert out.document.filename == "rapport.pdf"
    assert out.document.download_url == f"/generate/document/download/{lib_id}"


def test_serialize_result_document_kind_but_render_failed_no_block() -> None:
    # Tâche document dont le rendu a échoué fail-safe (worker n'a pas posé le
    # bloc) → output_kind="document" mais document=None.
    out = serialize_result(_make_result(metadata_json={"output_kind": "document"}))
    assert out.output_kind == "document"
    assert out.document is None


def test_serialize_result_legacy_none_metadata() -> None:
    # Tâche pré-feature : metadata_json None → generation + pas de doc.
    out = serialize_result(_make_result(metadata_json=None))
    assert out.output_kind == "generation"
    assert out.document is None
