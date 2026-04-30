"""J1 — Router RGPD tests (~12 tests)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user, require_admin
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    PermissionDeniedException,
    RateLimitAbuseException,
)
from app.main import app


def _fake_user(*, email="user@nexya.ai", id_=None) -> MagicMock:
    user = MagicMock()
    user.id = id_ or uuid.uuid4()
    user.email = email
    user.username = "testuser"
    user.is_active = True
    return user


@pytest.fixture
def fake_user():
    return _fake_user()


@pytest.fixture
def admin_user():
    return _fake_user(email="dpo@nexya.ai")


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def client(fake_user, fake_db):
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(admin_user, fake_db):
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user
    app.dependency_overrides[get_db] = lambda: fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════
# DATA EXPORT
# ══════════════════════════════════════════════════════════════


def test_data_export_returns_zip(monkeypatch, client, fake_user):
    monkeypatch.setattr("app.features.rgpd.router.check_user_rate_limit", AsyncMock())
    monkeypatch.setattr("app.features.rgpd.router.log_auth_event", AsyncMock())

    fake_result = MagicMock()
    fake_result.zip_bytes = b"PK\x03\x04fake-zip-bytes"
    fake_result.truncated = False
    fake_service = MagicMock()
    fake_service.build_export = AsyncMock(return_value=fake_result)
    monkeypatch.setattr(
        "app.features.rgpd.router.DataExportService",
        lambda: fake_service,
    )

    response = client.get("/rgpd/user/data-export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert str(fake_user.id) in response.headers["content-disposition"]
    assert response.headers["x-export-truncated"] == "false"
    assert response.content == b"PK\x03\x04fake-zip-bytes"


def test_data_export_rate_limited_returns_429(monkeypatch, client):
    async def _raise(*args, **kwargs):
        raise RateLimitAbuseException(retry_after=86400)

    monkeypatch.setattr("app.features.rgpd.router.check_user_rate_limit", _raise)
    response = client.get("/rgpd/user/data-export")
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "RATE_LIMIT_ABUSE"


# ══════════════════════════════════════════════════════════════
# CONSENT
# ══════════════════════════════════════════════════════════════


def test_list_consents_returns_active(monkeypatch, client):
    fake_consent = MagicMock()
    fake_consent.id = uuid.uuid4()
    fake_consent.consent_type = "tos"
    fake_consent.status = "granted"
    fake_consent.granted_at = datetime.now(UTC)
    fake_consent.revoked_at = None
    fake_consent.document_version = "tos-v1.0"
    fake_consent.document_hash = "a" * 64
    fake_consent.source = "register"

    monkeypatch.setattr(
        "app.features.rgpd.router.ConsentService.list_for_user",
        AsyncMock(return_value=[fake_consent]),
    )
    response = client.get("/rgpd/user/consent")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["items"]) == 1


def test_record_consent_returns_201(monkeypatch, client):
    fake_consent = MagicMock()
    fake_consent.id = uuid.uuid4()
    fake_consent.consent_type = "ai_processing"
    fake_consent.status = "granted"
    fake_consent.granted_at = datetime.now(UTC)
    fake_consent.revoked_at = None
    fake_consent.document_version = "ai-v1.0"
    fake_consent.document_hash = "b" * 64
    fake_consent.source = "settings_screen"

    monkeypatch.setattr(
        "app.features.rgpd.router.ConsentService.record",
        AsyncMock(return_value=fake_consent),
    )
    response = client.post(
        "/rgpd/user/consent",
        json={
            "action": "record",
            "consent_type": "ai_processing",
            "document_version": "ai-v1.0",
            "document_hash": "b" * 64,
            "source": "settings_screen",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["consent_type"] == "ai_processing"


def test_revoke_consent_returns_204(monkeypatch, client):
    monkeypatch.setattr(
        "app.features.rgpd.router.ConsentService.revoke",
        AsyncMock(return_value=None),
    )
    response = client.delete("/rgpd/user/consent/marketing_email")
    assert response.status_code == 204


def test_record_consent_invalid_hash_length(client):
    response = client.post(
        "/rgpd/user/consent",
        json={
            "action": "record",
            "consent_type": "tos",
            "document_version": "v1",
            "document_hash": "tooshort",
        },
    )
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# DELETION REQUEST
# ══════════════════════════════════════════════════════════════


def test_create_delete_request_returns_202(monkeypatch, client):
    fake_dr = MagicMock()
    fake_dr.id = uuid.uuid4()
    fake_dr.status = "pending"
    fake_dr.requested_at = datetime.now(UTC)
    fake_dr.scheduled_purge_at = datetime.now(UTC)
    fake_dr.purged_at = None
    fake_dr.reason = None

    monkeypatch.setattr(
        "app.features.rgpd.router.DeletionRequestService.create_request",
        AsyncMock(return_value=fake_dr),
    )
    response = client.post("/rgpd/user/account/delete-request", json={"reason": "I want out"})
    assert response.status_code == 202
    body = response.json()
    assert body["data"]["status"] == "pending"


def test_create_delete_request_409_if_already_pending(monkeypatch, client):
    from app.features.rgpd.deletion_service import (
        DeletionRequestAlreadyExistsException,
    )

    async def _raise(*args, **kwargs):
        raise DeletionRequestAlreadyExistsException()

    monkeypatch.setattr(
        "app.features.rgpd.router.DeletionRequestService.create_request",
        _raise,
    )
    response = client.post("/rgpd/user/account/delete-request", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "DELETION_REQUEST_ALREADY_EXISTS"


def test_cancel_delete_request_happy_path(monkeypatch, client):
    fake_dr = MagicMock()
    fake_dr.id = uuid.uuid4()
    fake_dr.status = "cancelled"
    fake_dr.requested_at = datetime.now(UTC)
    fake_dr.scheduled_purge_at = datetime.now(UTC)
    fake_dr.purged_at = None
    fake_dr.reason = None

    monkeypatch.setattr(
        "app.features.rgpd.router.DeletionRequestService.cancel_request",
        AsyncMock(return_value=fake_dr),
    )
    response = client.post("/rgpd/user/account/delete-request/cancel")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancelled"


# ══════════════════════════════════════════════════════════════
# ADMIN AI Act registry
# ══════════════════════════════════════════════════════════════


def test_admin_registry_csv_format(monkeypatch, admin_client):
    monkeypatch.setattr(
        "app.features.rgpd.router.AIActRegistryService.fetch_rows",
        AsyncMock(return_value=[]),
    )
    response = admin_client.get("/rgpd/admin/ai-act-registry?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    # BOM UTF-8 présent
    assert response.content.startswith(b"\xef\xbb\xbf")


def test_admin_registry_json_format(monkeypatch, admin_client):
    monkeypatch.setattr(
        "app.features.rgpd.router.AIActRegistryService.fetch_rows",
        AsyncMock(return_value=[]),
    )
    response = admin_client.get("/rgpd/admin/ai-act-registry?format=json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = json.loads(response.content)
    assert "exported_at" in body
    assert body["row_count"] == 0


def test_admin_registry_invalid_format_422(monkeypatch, admin_client):
    response = admin_client.get("/rgpd/admin/ai-act-registry?format=xml")
    assert response.status_code == 422


def test_admin_registry_non_admin_403(monkeypatch, fake_db):
    """Un user non-admin reçoit 403."""
    non_admin = _fake_user(email="random@user.com")

    async def _require_admin_raise():
        raise PermissionDeniedException()

    app.dependency_overrides[get_current_user] = lambda: non_admin
    app.dependency_overrides[require_admin] = _require_admin_raise
    app.dependency_overrides[get_db] = lambda: fake_db

    try:
        client = TestClient(app)
        response = client.get("/rgpd/admin/ai-act-registry")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
