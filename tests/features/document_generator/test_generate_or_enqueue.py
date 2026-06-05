"""Tests `DocumentGeneratorService.generate_or_enqueue` (C4.12).

Décision sync vs async selon la taille du markdown source.

Mock-first :
    - `_get_owned_message_content` mocké (retourne une source de N chars)
    - `generate` mocké (chemin sync)
    - `DocumentJobService.create_job` + `enqueue_document_generation` mockés
      (chemin async)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.document_tasks as document_tasks
from app.config import settings
from app.core.errors.exceptions import PlanRequiredException, ResourceNotFoundException
from app.features.document_generator.exceptions import DocumentSourceTooLongError
from app.features.document_generator.job_service import DocumentJobService
from app.features.document_generator.schemas import (
    DocumentGenerateRequest,
    DocumentGenerateResponse,
)
from app.features.document_generator.service import (
    DocumentAsyncResult,
    DocumentGeneratorService,
    DocumentSyncResult,
)

pytestmark = pytest.mark.asyncio


def _user(is_pro: bool = False):
    u = MagicMock()
    u.id = uuid.uuid4()
    u.is_pro = is_pro
    return u


def _body(*, remove_watermark: bool = False) -> DocumentGenerateRequest:
    return DocumentGenerateRequest(
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        format="pdf",
        template="minimal",
        remove_watermark=remove_watermark,
    )


def _fake_doc_response() -> DocumentGenerateResponse:
    now = datetime.now(timezone.utc)
    return DocumentGenerateResponse(
        library_id=uuid.uuid4(),
        download_url="https://x/y.pdf",
        filename="y.pdf",
        size_bytes=100,
        pages=1,
        truncated=False,
        expires_at=now,
        generated_at=now,
    )


def _mock_source(monkeypatch, length: int):
    monkeypatch.setattr(
        DocumentGeneratorService,
        "_get_owned_message_content",
        AsyncMock(return_value="a" * length),
    )


async def test_sync_when_under_threshold(monkeypatch):
    """Source courte → DocumentSyncResult (rendu immédiat)."""
    _mock_source(monkeypatch, length=settings.documents_generator_async_threshold_chars - 1)
    gen = AsyncMock(return_value=_fake_doc_response())
    monkeypatch.setattr(DocumentGeneratorService, "generate", gen)
    create = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "create_job", create)

    result = await DocumentGeneratorService.generate_or_enqueue(_user(), _body(), MagicMock())

    assert isinstance(result, DocumentSyncResult)
    gen.assert_awaited_once()
    create.assert_not_called()


async def test_async_when_over_threshold(monkeypatch):
    """Source longue → DocumentAsyncResult + job créé + enqueue."""
    _mock_source(monkeypatch, length=settings.documents_generator_async_threshold_chars + 5000)
    gen = AsyncMock()
    monkeypatch.setattr(DocumentGeneratorService, "generate", gen)
    fake_job = MagicMock()
    fake_job.id = uuid.uuid4()
    create = AsyncMock(return_value=fake_job)
    monkeypatch.setattr(DocumentJobService, "create_job", create)
    enqueue = AsyncMock()
    monkeypatch.setattr(document_tasks, "enqueue_document_generation", enqueue)

    result = await DocumentGeneratorService.generate_or_enqueue(_user(), _body(), MagicMock())

    assert isinstance(result, DocumentAsyncResult)
    assert result.job is fake_job
    gen.assert_not_called()  # rendu déporté, pas immédiat
    create.assert_awaited_once()
    enqueue.assert_awaited_once_with(fake_job.id)


async def test_kill_switch_forces_sync(monkeypatch):
    """async désactivé → sync même si source > seuil."""
    monkeypatch.setattr(settings, "documents_generator_async_enabled", False)
    _mock_source(monkeypatch, length=settings.documents_generator_async_threshold_chars + 50000)
    gen = AsyncMock(return_value=_fake_doc_response())
    monkeypatch.setattr(DocumentGeneratorService, "generate", gen)
    create = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "create_job", create)

    result = await DocumentGeneratorService.generate_or_enqueue(_user(), _body(), MagicMock())

    assert isinstance(result, DocumentSyncResult)
    gen.assert_awaited_once()
    create.assert_not_called()


async def test_pro_gate_blocks_free_remove_watermark(monkeypatch):
    """Free + remove_watermark=True → 403 PlanRequiredException AVANT fetch."""
    fetch = AsyncMock()
    monkeypatch.setattr(DocumentGeneratorService, "_get_owned_message_content", fetch)

    with pytest.raises(PlanRequiredException):
        await DocumentGeneratorService.generate_or_enqueue(
            _user(is_pro=False), _body(remove_watermark=True), MagicMock()
        )

    fetch.assert_not_called()  # gate pré-flight avant toute lecture


async def test_idor_404_propagates(monkeypatch):
    """Message non possédé → ResourceNotFoundException remonte."""
    monkeypatch.setattr(
        DocumentGeneratorService,
        "_get_owned_message_content",
        AsyncMock(side_effect=ResourceNotFoundException("Message")),
    )
    with pytest.raises(ResourceNotFoundException):
        await DocumentGeneratorService.generate_or_enqueue(_user(), _body(), MagicMock())


async def test_source_too_long_rejected_sync(monkeypatch):
    """Source > cap max_source_chars → 413 SYNCHRONE (pas un job qui échouera)."""
    _mock_source(monkeypatch, length=settings.documents_generator_max_source_chars + 1)
    create = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "create_job", create)

    with pytest.raises(DocumentSourceTooLongError):
        await DocumentGeneratorService.generate_or_enqueue(_user(), _body(), MagicMock())

    create.assert_not_called()  # pas d'enqueue d'un job voué à l'échec
