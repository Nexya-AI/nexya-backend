"""J1 — ConsentService unit tests (~12 tests)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.rgpd.consent_service import ConsentService
from app.features.rgpd.models import ConsentLog
from app.features.rgpd.schemas import ConsentRecordRequest


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
    return user


def _mk_existing_consent(user_id: uuid.UUID, **kwargs) -> ConsentLog:
    row = ConsentLog(
        user_id=user_id,
        consent_type=kwargs.get("consent_type", "tos"),
        status=kwargs.get("status", "granted"),
        document_version=kwargs.get("document_version", "tos-v1.0"),
        document_hash="a" * 64,
        source="register",
    )
    row.id = uuid.uuid4()
    row.granted_at = datetime.now(UTC)
    row.revoked_at = kwargs.get("revoked_at")
    return row


@pytest.mark.asyncio
async def test_record_happy_path_creates_granted(monkeypatch):
    user = _mk_user()
    body = ConsentRecordRequest(
        consent_type="tos",
        document_version="tos-v1.0",
        document_hash="a" * 64,
        source="register",
    )
    db = _mk_db(
        [
            _ScalarResult(one=None),  # no existing same-version
            MagicMock(),  # update revoke old (none)
        ]
    )
    monkeypatch.setattr(
        "app.features.rgpd.consent_service.log_auth_event",
        AsyncMock(),
    )
    row = await ConsentService.record(user, body, ip="1.2.3.4", user_agent="UA", db=db)
    assert row.status == "granted"
    assert row.consent_type == "tos"
    assert row.ip_address == "1.2.3.4"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_idempotent_same_version_returns_existing(monkeypatch):
    user = _mk_user()
    existing = _mk_existing_consent(user.id, document_version="tos-v1.0")
    body = ConsentRecordRequest(
        consent_type="tos",
        document_version="tos-v1.0",
        document_hash="a" * 64,
    )
    db = _mk_db([_ScalarResult(one=existing)])
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", AsyncMock())
    row = await ConsentService.record(user, body, ip=None, user_agent=None, db=db)
    assert row is existing
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_record_new_version_revokes_old(monkeypatch):
    user = _mk_user()
    body = ConsentRecordRequest(
        consent_type="tos",
        document_version="tos-v2.0",
        document_hash="b" * 64,
    )
    db = _mk_db(
        [
            _ScalarResult(one=None),  # no existing same v2
            MagicMock(),  # UPDATE revoke old v1
        ]
    )
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", AsyncMock())
    row = await ConsentService.record(user, body, ip=None, user_agent=None, db=db)
    assert row.document_version == "tos-v2.0"
    # 2 execute calls: select existing + update old granted
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_revoke_happy_path(monkeypatch):
    user = _mk_user()
    active = _mk_existing_consent(user.id, consent_type="marketing_email")
    db = _mk_db([_ScalarResult(one=active)])
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", AsyncMock())
    row = await ConsentService.revoke(user, "marketing_email", ip="1.1.1.1", user_agent=None, db=db)
    assert row is not None
    assert row.status == "revoked"
    assert active.revoked_at is not None  # ancien granted maintenant flagué
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_no_active_returns_none(monkeypatch):
    user = _mk_user()
    db = _mk_db([_ScalarResult(one=None)])
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", AsyncMock())
    row = await ConsentService.revoke(user, "tos", ip=None, user_agent=None, db=db)
    assert row is None
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_is_granted_true():
    user_id = uuid.uuid4()
    db = _mk_db([_ScalarResult(one=uuid.uuid4())])
    assert await ConsentService.is_granted(user_id, "ai_processing", db) is True


@pytest.mark.asyncio
async def test_is_granted_false():
    user_id = uuid.uuid4()
    db = _mk_db([_ScalarResult(one=None)])
    assert await ConsentService.is_granted(user_id, "ai_processing", db) is False


@pytest.mark.asyncio
async def test_list_for_user_returns_active_only():
    user = _mk_user()
    rows = [
        _mk_existing_consent(user.id, consent_type="tos"),
        _mk_existing_consent(user.id, consent_type="ai_processing"),
    ]
    db = _mk_db([_ScalarResult(all_rows=rows)])
    result = await ConsentService.list_for_user(user, db)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_history_includes_revoked():
    user = _mk_user()
    rows = [
        _mk_existing_consent(user.id, consent_type="tos"),
        _mk_existing_consent(
            user.id,
            consent_type="marketing_email",
            status="revoked",
            revoked_at=datetime.now(UTC),
        ),
    ]
    db = _mk_db([_ScalarResult(all_rows=rows)])
    result = await ConsentService.list_history_for_user(user.id, db)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_record_audit_consent_granted(monkeypatch):
    user = _mk_user()
    body = ConsentRecordRequest(
        consent_type="tos",
        document_version="tos-v1.0",
        document_hash="a" * 64,
    )
    db = _mk_db([_ScalarResult(one=None), MagicMock()])
    audit = AsyncMock()
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", audit)
    await ConsentService.record(user, body, ip=None, user_agent=None, db=db)
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_type"] == "consent_granted"


@pytest.mark.asyncio
async def test_revoke_audit_consent_revoked(monkeypatch):
    user = _mk_user()
    active = _mk_existing_consent(user.id, consent_type="ai_processing")
    db = _mk_db([_ScalarResult(one=active)])
    audit = AsyncMock()
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", audit)
    await ConsentService.revoke(user, "ai_processing", ip=None, user_agent=None, db=db)
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["event_type"] == "consent_revoked"


@pytest.mark.asyncio
async def test_record_ip_user_agent_tracked(monkeypatch):
    user = _mk_user()
    body = ConsentRecordRequest(
        consent_type="ai_processing",
        document_version="ai-v1.0",
        document_hash="c" * 64,
    )
    db = _mk_db([_ScalarResult(one=None), MagicMock()])
    monkeypatch.setattr("app.features.rgpd.consent_service.log_auth_event", AsyncMock())
    row = await ConsentService.record(
        user, body, ip="9.9.9.9", user_agent="Mozilla/5.0 NEXYA Test", db=db
    )
    assert row.ip_address == "9.9.9.9"
    assert "NEXYA Test" in row.user_agent
