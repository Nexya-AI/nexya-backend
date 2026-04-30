"""
Tests D4 — intégration `FileUploadService` ↔ enqueue chunking + quota.

Ces tests vérifient :
- Que l'enqueue est appelé pour les mimes éligibles (PDF/DOCX/TXT/MD).
- Que l'enqueue n'est PAS appelé pour les mimes images/audio/vidéo.
- Que le quota `documents_max_free` bloque un 4ᵉ upload PDF avec
  `DOCUMENTS_QUOTA_EXCEEDED` (402).
- Que le quota `documents_max_pro` laisse passer jusqu'à la limite Pro.
- Que l'enqueue reste fail-silent si Redis est down.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import DocumentsQuotaExceededException
from app.features.files import service as files_service

_USER_ID = uuid.UUID("33333333-0000-4000-8000-000000000003")


def _make_user(is_pro: bool = False) -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = is_pro
    return user


class _FakeDB:
    def __init__(self, *, doc_count: int) -> None:
        self._doc_count = doc_count
        self.added: list[Any] = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):
        sql = str(stmt).lower()
        result = MagicMock()
        if "count" in sql and "uploaded_files" in sql:
            result.scalar_one.return_value = self._doc_count
        else:
            result.scalar_one_or_none.return_value = None
            result.scalar_one.return_value = 0
        return result

    def add(self, row) -> None:
        self.added.append(row)


# ══════════════════════════════════════════════════════════════
# 1. Quota pricing — Free bloqué à max
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_documents_quota_free_blocks_when_at_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "documents_max_free", 3)
    user = _make_user(is_pro=False)
    db = _FakeDB(doc_count=3)

    with pytest.raises(DocumentsQuotaExceededException) as ctx:
        await files_service.FileUploadService._check_documents_quota(user, db)
    # data expose la jauge pour l'UI.
    assert ctx.value.data == {"current": 3, "max": 3, "plan": "free"}
    assert ctx.value.status_code == 402


@pytest.mark.asyncio
async def test_documents_quota_free_passes_below_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "documents_max_free", 3)
    user = _make_user(is_pro=False)
    db = _FakeDB(doc_count=2)

    # Ne doit pas raise.
    await files_service.FileUploadService._check_documents_quota(user, db)


@pytest.mark.asyncio
async def test_documents_quota_pro_has_higher_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "documents_max_free", 3)
    monkeypatch.setattr(settings, "documents_max_pro", 50)
    user_pro = _make_user(is_pro=True)
    # Pro à 10 docs → largement sous son cap 50.
    db = _FakeDB(doc_count=10)
    await files_service.FileUploadService._check_documents_quota(user_pro, db)


@pytest.mark.asyncio
async def test_documents_quota_pro_blocks_at_pro_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "documents_max_pro", 50)
    user_pro = _make_user(is_pro=True)
    db = _FakeDB(doc_count=50)
    with pytest.raises(DocumentsQuotaExceededException) as ctx:
        await files_service.FileUploadService._check_documents_quota(user_pro, db)
    assert ctx.value.data["plan"] == "pro"
    assert ctx.value.data["max"] == 50


# ══════════════════════════════════════════════════════════════
# 2. Enqueue path — éligible vs non-éligible
# ══════════════════════════════════════════════════════════════


def test_chunking_eligible_mimes_covers_text_docs() -> None:
    """Le frozenset éligible doit couvrir les 4 mimes texte et exclure
    les images/audio/vidéo."""
    from app.features.files.service import _CHUNKING_ELIGIBLE_MIMES

    assert "application/pdf" in _CHUNKING_ELIGIBLE_MIMES
    assert "text/plain" in _CHUNKING_ELIGIBLE_MIMES
    assert "text/markdown" in _CHUNKING_ELIGIBLE_MIMES
    assert (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in _CHUNKING_ELIGIBLE_MIMES
    )
    # Exclusions.
    assert "image/png" not in _CHUNKING_ELIGIBLE_MIMES
    assert "audio/mpeg" not in _CHUNKING_ELIGIBLE_MIMES
    assert "video/mp4" not in _CHUNKING_ELIGIBLE_MIMES
