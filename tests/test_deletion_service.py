"""J1 — DeletionRequestService unit tests (~8 tests)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.rgpd.deletion_service import (
    DeletionRequestAlreadyExistsException,
    DeletionRequestService,
    NoActiveDeletionRequestException,
)
from app.features.rgpd.models import DeletionRequest


class _ScalarResult:
    def __init__(self, *, one=None, all_rows=None):
        self._one = one
        self._all = all_rows or []

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return self._all


def _mk_db(execute_results: list) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute_results)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    return db


def _mk_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "real@user.com"
    user.username = "realuser"
    user.display_name = "Real User"
    user.avatar_url = "http://x"
    user.bio = "hello"
    user.is_active = True
    user.deleted_at = None
    return user


def _mk_existing_request(user_id, status="pending"):
    req = MagicMock(spec=DeletionRequest)
    req.id = uuid.uuid4()
    req.user_id = user_id
    req.status = status
    req.purge_summary_json = None
    req.updated_at = None
    return req


@pytest.mark.asyncio
async def test_create_request_happy_path_anonymizes_user(monkeypatch):
    user = _mk_user()
    original_email = user.email
    db = _mk_db([_ScalarResult(one=None)])  # no existing
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", AsyncMock())

    request = await DeletionRequestService.create_request(
        user, reason="test", ip="1.2.3.4", user_agent="UA", db=db
    )
    # User anonymisé
    assert user.email != original_email
    assert user.email.startswith("deleted_")
    assert user.is_active is False
    assert user.deleted_at is not None
    # Email original capturé dans purge_summary_json
    db.add.assert_called_once()
    request_added = db.add.call_args.args[0]
    assert request_added.purge_summary_json["email_for_confirmation"] == original_email
    # Status pending
    assert request_added.status == "pending"


@pytest.mark.asyncio
async def test_create_request_idempotent_409_if_pending_exists(monkeypatch):
    user = _mk_user()
    existing = _mk_existing_request(user.id, status="pending")
    db = _mk_db([_ScalarResult(one=existing)])
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", AsyncMock())

    with pytest.raises(DeletionRequestAlreadyExistsException) as exc:
        await DeletionRequestService.create_request(
            user, reason=None, ip=None, user_agent=None, db=db
        )
    assert exc.value.status_code == 409
    assert exc.value.code == "DELETION_REQUEST_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_cancel_request_happy_path_restores_user(monkeypatch):
    user = _mk_user()
    user.is_active = False
    user.deleted_at = datetime.now(UTC)
    pending = _mk_existing_request(user.id, status="pending")
    db = _mk_db([_ScalarResult(one=pending)])
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", AsyncMock())

    request = await DeletionRequestService.cancel_request(user, ip=None, user_agent=None, db=db)
    assert request.status == "cancelled"
    assert user.is_active is True
    assert user.deleted_at is None


@pytest.mark.asyncio
async def test_cancel_request_404_if_no_active(monkeypatch):
    user = _mk_user()
    db = _mk_db([_ScalarResult(one=None)])
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", AsyncMock())
    with pytest.raises(NoActiveDeletionRequestException):
        await DeletionRequestService.cancel_request(user, ip=None, user_agent=None, db=db)


@pytest.mark.asyncio
async def test_cancel_request_404_if_processing_not_pending(monkeypatch):
    """Si le status est 'processing' (cron en cours), trop tard pour annuler."""
    user = _mk_user()
    processing = _mk_existing_request(user.id, status="processing")
    db = _mk_db([_ScalarResult(one=processing)])
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", AsyncMock())
    with pytest.raises(NoActiveDeletionRequestException):
        await DeletionRequestService.cancel_request(user, ip=None, user_agent=None, db=db)


@pytest.mark.asyncio
async def test_create_request_audits_account_delete_requested(monkeypatch):
    user = _mk_user()
    db = _mk_db([_ScalarResult(one=None)])
    audit = AsyncMock()
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", audit)
    await DeletionRequestService.create_request(
        user, reason=None, ip="1.1.1.1", user_agent=None, db=db
    )
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_type"] == "account_delete_requested"


@pytest.mark.asyncio
async def test_cancel_request_audits_account_delete_cancelled(monkeypatch):
    user = _mk_user()
    pending = _mk_existing_request(user.id, status="pending")
    db = _mk_db([_ScalarResult(one=pending)])
    audit = AsyncMock()
    monkeypatch.setattr("app.features.rgpd.deletion_service.log_auth_event", audit)
    await DeletionRequestService.cancel_request(user, ip=None, user_agent=None, db=db)
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_type"] == "account_delete_cancelled"


@pytest.mark.asyncio
async def test_mark_completed_merges_summary():
    request = _mk_existing_request(uuid.uuid4(), status="processing")
    request.purge_summary_json = {"email_for_confirmation": "x@y.z"}
    db = MagicMock()
    db.flush = AsyncMock()
    await DeletionRequestService.mark_completed(
        request,
        purge_summary={"tables_purged": 22, "blobs_deleted": 5},
        db=db,
    )
    assert request.status == "completed"
    assert request.purge_summary_json["email_for_confirmation"] == "x@y.z"
    assert request.purge_summary_json["tables_purged"] == 22
    assert request.purge_summary_json["blobs_deleted"] == 5
