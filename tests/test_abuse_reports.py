"""
Tests — endpoint POST /chat/reports + ReportService (Lot 5).

Couverture :
1. ReportService — happy-path (insert + commit + log)
2. ReportService — message non possédé → 404 IDOR-safe
3. ReportService — message inexistant → 404
4. ReportService — IntegrityError (UNIQUE violation) → 409 DUPLICATE_REPORT
5. Router — 201 + AbuseReportResponse correctement formé
6. Router — 422 sur reason invalide (Literal Pydantic)
7. Router — 422 sur message_id non-UUID (Pydantic)
8. Router — 404 cascadé depuis le service
9. Router — 409 cascadé depuis DuplicateReportException
10. Router — 429 RATE_LIMIT_ABUSE quand le rate limiter lève

Discipline tests : aucun Redis, aucune DB. Tout en monkeypatch + AsyncMock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    DuplicateReportException,
    RateLimitAbuseException,
    ResourceNotFoundException,
)
from app.features.auth.models import User
from app.features.chat import router as chat_router_module
from app.features.chat.models import AbuseReport, Message
from app.features.chat.schemas import AbuseReportCreate
from app.features.chat.service import ReportService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_CONV_ID = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000042")
_FAKE_MESSAGE_ID = uuid.UUID("5b59c0a7-1b2c-4d5e-8f7a-9b0c1d2e3f4a")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_message() -> Message:
    now = datetime(2026, 4, 21, 14, 31, 0, tzinfo=UTC)
    msg = Message(
        conversation_id=_FAKE_CONV_ID,
        role="assistant",
        content="...",
        status="completed",
    )
    msg.id = _FAKE_MESSAGE_ID
    msg.created_at = now
    msg.updated_at = now
    return msg


def _make_fake_report() -> AbuseReport:
    now = datetime(2026, 4, 21, 14, 32, 0, tzinfo=UTC)
    report = AbuseReport(
        user_id=_FAKE_USER_ID,
        message_id=_FAKE_MESSAGE_ID,
        conversation_id=_FAKE_CONV_ID,
        reason="offensive",
        detail=None,
    )
    report.id = uuid.UUID("99999999-9999-4999-8999-999999999999")
    report.status = "pending"
    report.created_at = now
    report.updated_at = now
    return report


@pytest.fixture
def client() -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _fake_user_override() -> User:
        return fake_user

    async def _fake_db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _fake_user_override
    app.dependency_overrides[get_db] = _fake_db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# Service — ReportService.create_report
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_report_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Insert + commit + denormalisation conversation_id depuis le message."""
    user = _make_fake_user()
    message = _make_fake_message()

    mock_owned = AsyncMock(return_value=message)
    monkeypatch.setattr(ReportService, "_get_owned_message", mock_owned)

    added: list[AbuseReport] = []
    db = MagicMock()
    db.add = MagicMock(side_effect=lambda r: added.append(r))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    body = AbuseReportCreate(
        message_id=_FAKE_MESSAGE_ID, reason="offensive", detail="Insultes"
    )
    report = await ReportService.create_report(user, body, db)

    assert len(added) == 1
    assert report is added[0]
    assert report.user_id == _FAKE_USER_ID
    assert report.message_id == _FAKE_MESSAGE_ID
    # Conversation_id récupéré sur le message → pas de second SELECT.
    assert report.conversation_id == _FAKE_CONV_ID
    assert report.reason == "offensive"
    assert report.detail == "Insultes"
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    mock_owned.assert_awaited_once_with(_FAKE_MESSAGE_ID, user.id, db)


@pytest.mark.asyncio
async def test_create_report_propagates_404_when_message_not_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un message qui n'appartient pas au user → ResourceNotFoundException
    levée par `_get_owned_message`, pas d'INSERT, pas de commit."""
    user = _make_fake_user()
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    monkeypatch.setattr(
        ReportService,
        "_get_owned_message",
        AsyncMock(side_effect=ResourceNotFoundException("Message")),
    )

    body = AbuseReportCreate(message_id=_FAKE_MESSAGE_ID, reason="offensive")
    with pytest.raises(ResourceNotFoundException) as exc_info:
        await ReportService.create_report(user, body, db)

    assert exc_info.value.code == "RESOURCE_NOT_FOUND"
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_report_translates_integrity_error_into_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNIQUE (user_id, message_id) violé → DuplicateReportException 409."""
    user = _make_fake_user()
    message = _make_fake_message()

    monkeypatch.setattr(
        ReportService,
        "_get_owned_message",
        AsyncMock(return_value=message),
    )

    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock(
        side_effect=IntegrityError("INSERT", params={}, orig=Exception("UNIQUE"))
    )
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()

    body = AbuseReportCreate(message_id=_FAKE_MESSAGE_ID, reason="dangerous")
    with pytest.raises(DuplicateReportException) as exc_info:
        await ReportService.create_report(user, body, db)

    assert exc_info.value.code == "DUPLICATE_REPORT"
    assert exc_info.value.status_code == 409
    db.rollback.assert_awaited_once()
    db.refresh.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# Router — POST /chat/reports
# ══════════════════════════════════════════════════════════════

def _patch_rate_limit_noop(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Court-circuite le rate limiter (autorise tout)."""
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_router_module, "rate_limit_abuse_reports", mock)
    return mock


def test_post_reports_returns_201_with_response_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy-path : 201 + payload `NexyaResponse[AbuseReportResponse]`."""
    _patch_rate_limit_noop(monkeypatch)

    report = _make_fake_report()
    monkeypatch.setattr(
        ReportService,
        "create_report",
        AsyncMock(return_value=report),
    )

    response = client.post(
        "/chat/reports",
        json={
            "message_id": str(_FAKE_MESSAGE_ID),
            "reason": "offensive",
            "detail": "Contenu insultant",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(report.id)
    assert body["data"]["message_id"] == str(_FAKE_MESSAGE_ID)
    assert body["data"]["conversation_id"] == str(_FAKE_CONV_ID)
    assert body["data"]["reason"] == "offensive"
    assert body["data"]["status"] == "pending"


def test_post_reports_rejects_invalid_reason(client: TestClient) -> None:
    """`reason` hors Literal → 422 sans même atteindre le service."""
    response = client.post(
        "/chat/reports",
        json={
            "message_id": str(_FAKE_MESSAGE_ID),
            "reason": "not-a-real-reason",
        },
    )
    assert response.status_code == 422


def test_post_reports_rejects_non_uuid_message_id(client: TestClient) -> None:
    """`message_id` mal formé → 422 Pydantic."""
    response = client.post(
        "/chat/reports",
        json={"message_id": "not-an-uuid", "reason": "offensive"},
    )
    assert response.status_code == 422


def test_post_reports_returns_404_when_message_not_owned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service lève ResourceNotFoundException → 404 RESOURCE_NOT_FOUND."""
    _patch_rate_limit_noop(monkeypatch)

    monkeypatch.setattr(
        ReportService,
        "create_report",
        AsyncMock(side_effect=ResourceNotFoundException("Message")),
    )

    response = client.post(
        "/chat/reports",
        json={"message_id": str(_FAKE_MESSAGE_ID), "reason": "offensive"},
    )
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "RESOURCE_NOT_FOUND"


def test_post_reports_returns_409_on_duplicate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doublon (UNIQUE) → 409 DUPLICATE_REPORT, message neutre."""
    _patch_rate_limit_noop(monkeypatch)

    monkeypatch.setattr(
        ReportService,
        "create_report",
        AsyncMock(side_effect=DuplicateReportException()),
    )

    response = client.post(
        "/chat/reports",
        json={"message_id": str(_FAKE_MESSAGE_ID), "reason": "offensive"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "DUPLICATE_REPORT"


def test_post_reports_returns_429_when_rate_limit_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le rate limiter lève → 429 RATE_LIMIT_ABUSE avec retry_after."""
    monkeypatch.setattr(
        chat_router_module,
        "rate_limit_abuse_reports",
        AsyncMock(side_effect=RateLimitAbuseException(retry_after=1800)),
    )
    create_mock = AsyncMock()
    monkeypatch.setattr(ReportService, "create_report", create_mock)

    response = client.post(
        "/chat/reports",
        json={"message_id": str(_FAKE_MESSAGE_ID), "reason": "offensive"},
    )
    assert response.status_code == 429
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "RATE_LIMIT_ABUSE"
    assert body["data"]["retry_after"] == 1800
    create_mock.assert_not_awaited()
