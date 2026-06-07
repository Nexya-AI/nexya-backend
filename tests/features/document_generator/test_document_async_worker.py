"""Tests worker `generate_document_async` (C4.12).

Mock-first :
    - `AsyncSessionLocal` → fake async context manager
    - `db.get` → side_effect [job, user]
    - DocumentJobService.mark_* + DocumentGeneratorService.generate mockés
    - NotificationDispatcher.dispatch mocké (assert category='documents')
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.document_tasks as document_tasks
from app.features.document_generator.exceptions import DocumentRenderFailedError
from app.features.document_generator.job_service import DocumentJobService
from app.features.document_generator.schemas import DocumentGenerateResponse
from app.features.document_generator.service import DocumentGeneratorService

pytestmark = pytest.mark.asyncio


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _make_job(*, status="queued", deleted=False, fmt="pdf"):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.user_id = uuid.uuid4()
    job.conversation_id = uuid.uuid4()
    job.message_id = uuid.uuid4()
    job.status = status
    job.format = fmt
    job.template = "minimal"
    job.params_json = {"options": {}, "remove_watermark": False}
    job.deleted_at = (datetime.now(UTC) if deleted else None)
    return job


def _fake_doc_response():
    now = datetime.now(UTC)
    return DocumentGenerateResponse(
        library_id=uuid.uuid4(),
        download_url="https://x/y.pdf",
        filename="y.pdf",
        size_bytes=4242,
        pages=7,
        truncated=False,
        expires_at=now,
        generated_at=now,
    )


def _install(monkeypatch, *, job, user=None, get_side_effect=None):
    """Pose AsyncSessionLocal + db.get + un user par défaut."""
    db = MagicMock()
    if get_side_effect is not None:
        db.get = AsyncMock(side_effect=get_side_effect)
    else:
        fake_user = user if user is not None else MagicMock()
        db.get = AsyncMock(side_effect=[job, fake_user])
    monkeypatch.setattr(document_tasks, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    dispatch = AsyncMock()
    monkeypatch.setattr(document_tasks.NotificationDispatcher, "dispatch", dispatch)
    return db, dispatch


async def test_happy_path_marks_done_and_dispatches(monkeypatch):
    job = _make_job()
    _, dispatch = _install(monkeypatch, job=job)
    monkeypatch.setattr(DocumentJobService, "mark_processing", AsyncMock(return_value=True))
    mark_done = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "mark_done", mark_done)
    monkeypatch.setattr(
        DocumentGeneratorService, "generate", AsyncMock(return_value=_fake_doc_response())
    )

    out = await document_tasks.generate_document_async({}, str(job.id))

    assert out["status"] == "done"
    mark_done.assert_awaited_once()
    dispatch.assert_awaited_once()
    # Push de la bonne catégorie + source_kind + deep link conv.
    kwargs = dispatch.await_args.kwargs
    assert kwargs["category"] == "documents"
    assert kwargs["source_kind"] == "document_generator"
    assert kwargs["data"]["deep_link"] == f"nexya://chat/{job.conversation_id}"
    assert kwargs["data"]["subtype"] == "document_ready"


async def test_render_failure_marks_failed_and_dispatches(monkeypatch):
    job = _make_job()
    _, dispatch = _install(monkeypatch, job=job)
    monkeypatch.setattr(DocumentJobService, "mark_processing", AsyncMock(return_value=True))
    mark_failed = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "mark_failed", mark_failed)
    monkeypatch.setattr(
        DocumentGeneratorService,
        "generate",
        AsyncMock(side_effect=DocumentRenderFailedError("WeasyPrint timeout")),
    )

    out = await document_tasks.generate_document_async({}, str(job.id))

    assert out["status"] == "failed"
    assert out["error_code"] == "DOCUMENT_RENDER_FAILED"
    mark_failed.assert_awaited_once()
    assert mark_failed.await_args.kwargs["error_code"] == "DOCUMENT_RENDER_FAILED"
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["data"]["subtype"] == "document_failed"


async def test_unexpected_exception_failsafe(monkeypatch):
    """Exception non typée → mark_failed DOCUMENT_GENERATION_FAILED, aucune
    exception ne remonte (fail-safe absolu, pas de retry arq infini)."""
    job = _make_job()
    _, dispatch = _install(monkeypatch, job=job)
    monkeypatch.setattr(DocumentJobService, "mark_processing", AsyncMock(return_value=True))
    mark_failed = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "mark_failed", mark_failed)
    monkeypatch.setattr(
        DocumentGeneratorService, "generate", AsyncMock(side_effect=RuntimeError("boom"))
    )

    out = await document_tasks.generate_document_async({}, str(job.id))

    assert out["status"] == "failed"
    assert out["error_code"] == "DOCUMENT_GENERATION_FAILED"
    mark_failed.assert_awaited_once()


async def test_idempotent_skip_when_not_queued(monkeypatch):
    """Double-livraison arq : mark_processing False → skip sans rendu."""
    job = _make_job(status="processing")
    _install(monkeypatch, job=job)
    monkeypatch.setattr(DocumentJobService, "mark_processing", AsyncMock(return_value=False))
    gen = AsyncMock()
    monkeypatch.setattr(DocumentGeneratorService, "generate", gen)

    out = await document_tasks.generate_document_async({}, str(job.id))

    assert out["skipped"] is True
    assert out["reason"] == "not_queued"
    gen.assert_not_called()


async def test_skip_when_job_missing(monkeypatch):
    """db.get → None (job purgé) → skip missing."""
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    monkeypatch.setattr(document_tasks, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    out = await document_tasks.generate_document_async({}, str(uuid.uuid4()))
    assert out["skipped"] is True
    assert out["reason"] == "missing"


async def test_user_missing_marks_failed(monkeypatch):
    """User purgé RGPD entre l'enqueue et l'exécution → mark_failed."""
    job = _make_job()
    _install(monkeypatch, job=job, get_side_effect=[job, None])
    monkeypatch.setattr(DocumentJobService, "mark_processing", AsyncMock(return_value=True))
    mark_failed = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "mark_failed", mark_failed)

    out = await document_tasks.generate_document_async({}, str(job.id))

    assert out["skipped"] is True
    assert out["reason"] == "user_missing"
    assert mark_failed.await_args.kwargs["error_code"] == "USER_NOT_FOUND"


async def test_invalid_params_marks_failed(monkeypatch):
    """params_json incohérent (format invalide) → mark_failed VALIDATION_ERROR."""
    job = _make_job(fmt="tiff")  # hors Literal pdf/docx
    _install(monkeypatch, job=job)
    monkeypatch.setattr(DocumentJobService, "mark_processing", AsyncMock(return_value=True))
    mark_failed = AsyncMock()
    monkeypatch.setattr(DocumentJobService, "mark_failed", mark_failed)
    gen = AsyncMock()
    monkeypatch.setattr(DocumentGeneratorService, "generate", gen)

    out = await document_tasks.generate_document_async({}, str(job.id))

    assert out["skipped"] is True
    assert out["reason"] == "params_invalid"
    gen.assert_not_called()
    assert mark_failed.await_args.kwargs["error_code"] == "VALIDATION_ERROR"
