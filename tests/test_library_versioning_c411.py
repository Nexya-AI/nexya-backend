"""C4.11 — Tests unitaires versioning lineage `library_items.parent_library_id`.

Couverture focused 5 tests (decision Ivan + budget temps strict) :

  1. `test_extract_version_number_fallback_1_for_legacy_items` — items
     pré-C4.11 sans metadata_json.version_number → fallback 1.
  2. `test_extract_version_number_reads_from_metadata` — items avec
     metadata_json.version_number=3 → retourne 3.
  3. `test_count_versions_for_lineage_root_alone_returns_1` — racine
     sans descendants → versions_count=1 (l'item est seul dans son lineage).
  4. `test_resolve_versioning_root_detects_souple` — un doc existe pour
     (user, source_message_id) → DocumentGeneratorService retourne son id
     (decision Ivan : detection SOUPLE, file_type IGNORÉ → PDF+DOCX du
     même message = même lineage).
  5. `test_resolve_versioning_root_returns_none_for_first_doc` — aucun
     doc existant pour (user, source_message_id) → retourne None (c'est
     la première génération, l'item à créer sera la racine v1).

Pattern strict aligné `tests/test_library_service.py` C3 (mocks
AsyncSession + factory `_make_item`, zéro container Postgres requis).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.document_generator.service import DocumentGeneratorService
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService


# ══════════════════════════════════════════════════════════════
# Helpers / fixtures
# ══════════════════════════════════════════════════════════════


def _make_item(
    *,
    item_id: uuid.UUID | None = None,
    parent_library_id: uuid.UUID | None = None,
    metadata_json: dict | None = None,
) -> LibraryItem:
    """Factory minimal pour tests — pattern aligné test_library_service.py."""
    now = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)
    item = LibraryItem(
        user_id=uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77"),
        type="document",
        title="Test doc",
        storage_key="c4a2.../library/document/ab/abcd.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        content_sha256="a" * 64,
        source="generated",
    )
    item.id = item_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    item.created_at = now
    item.updated_at = now
    item.deleted_at = None
    item.file_type = "pdf"
    item.description = None
    item.width_px = None
    item.height_px = None
    item.duration_ms = None
    item.aspect_ratio = None
    item.provider = "weasyprint"
    item.model = "template_school"
    item.prompt = None
    item.source_conversation_id = None
    item.source_message_id = None
    item.parent_library_id = parent_library_id
    item.tags = None
    item.metadata_json = metadata_json
    return item


class _ScalarResult:
    """Mock minimal de `Result` SQLAlchemy."""

    def __init__(self, *, scalar_value: object | None = None) -> None:
        self._value = scalar_value

    def scalar_one(self) -> object | None:
        return self._value

    def scalar_one_or_none(self) -> object | None:
        return self._value


# ══════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════


def test_extract_version_number_fallback_1_for_legacy_items() -> None:
    """Items pré-C4.11 sans metadata_json → fallback 1 (rétro-compat)."""
    # Cas 1 : metadata_json = None (item legacy avant C4.11)
    legacy_item = _make_item(metadata_json=None)
    assert LibraryService._extract_version_number(legacy_item) == 1

    # Cas 2 : metadata_json existe mais SANS clé version_number (item
    # C4.7 par exemple avec template/has_watermark mais pas C4.11)
    partial_item = _make_item(
        metadata_json={"template": "school", "has_watermark": True}
    )
    assert LibraryService._extract_version_number(partial_item) == 1

    # Cas 3 : version_number présent mais valeur pathologique → fallback 1
    bad_item = _make_item(metadata_json={"version_number": "not-an-int"})
    assert LibraryService._extract_version_number(bad_item) == 1

    # Cas 4 : version_number=None explicite (étrange mais possible) → 1
    null_item = _make_item(metadata_json={"version_number": None})
    assert LibraryService._extract_version_number(null_item) == 1


def test_extract_version_number_reads_from_metadata() -> None:
    """Items C4.11 avec metadata_json.version_number=N → retourne N."""
    item_v1 = _make_item(metadata_json={"version_number": 1})
    assert LibraryService._extract_version_number(item_v1) == 1

    item_v3 = _make_item(metadata_json={"version_number": 3})
    assert LibraryService._extract_version_number(item_v3) == 3

    # Cas string castable (cohérent avec parsing tolérant)
    item_str = _make_item(metadata_json={"version_number": "5"})
    assert LibraryService._extract_version_number(item_str) == 5


@pytest.mark.asyncio
async def test_count_versions_for_lineage_root_alone_returns_1() -> None:
    """Racine sans descendants → versions_count=1 (l'item est seul)."""
    item = _make_item(parent_library_id=None)
    db = MagicMock()
    # SELECT COUNT retourne 1 (juste la racine elle-même).
    db.execute = AsyncMock(return_value=_ScalarResult(scalar_value=1))

    count = await LibraryService.count_versions_for_lineage(item, db)
    assert count == 1
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_versioning_root_detects_souple() -> None:
    """Detection SOUPLE : doc existant pour (user, source_message_id) → return id.

    C'est la régénération typique : l'user a déjà généré ce message en PDF,
    il le régénère en DOCX → le DOCX sera v2 du même lineage (file_type IGNORÉ).
    """
    root_id = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000001")
    user_id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    source_message_id = uuid.UUID("cccccccc-0000-4000-8000-000000000001")

    db = MagicMock()
    db.execute = AsyncMock(return_value=_ScalarResult(scalar_value=root_id))

    result = await DocumentGeneratorService._resolve_versioning_root(
        user_id=user_id,
        source_message_id=source_message_id,
        db=db,
    )
    assert result == root_id
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_versioning_root_returns_none_for_first_doc() -> None:
    """Aucun doc existant pour ce message → return None (sera racine v1)."""
    user_id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    source_message_id = uuid.UUID("dddddddd-0000-4000-8000-000000000001")

    db = MagicMock()
    db.execute = AsyncMock(return_value=_ScalarResult(scalar_value=None))

    result = await DocumentGeneratorService._resolve_versioning_root(
        user_id=user_id,
        source_message_id=source_message_id,
        db=db,
    )
    assert result is None
    assert db.execute.await_count == 1
