"""
Tests F3 — router /notifications + /user/notification-preferences + unsubscribe.

Couvre :
- GET /notifications 200 + enveloppe NexyaResponse[NotificationsPage].
- GET forward filtres query au service.
- POST /notifications/read 200 + count retourné.
- POST /read 422 sur IDs dupliqués / liste vide / >100 items.
- DELETE /notifications/{id} 204 + 404 IDOR-safe.
- GET /user/notification-preferences retourne les 5 catégories.
- PUT /user/notification-preferences UPSERT + 422 doublon catégorie.
- POST /notifications/unsubscribe/{token} 200 pose channel='none'.
- POST /unsubscribe token invalide → 400 UNSUBSCRIBE_TOKEN_INVALID.
- POST /unsubscribe catégorie security → 400 UNSUBSCRIBE_SECURITY_REFUSED.
- POST /unsubscribe rate limit IP → 429 sur 11ᵉ appel.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.auth.unsubscribe_tokens import create_unsubscribe_token
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.notifications.preferences import PreferenceEntry
from app.features.notifications.service import NotificationsPageOrm
from app.main import app


@pytest.fixture
def fake_user():
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.email = "ivan@nexya.ai"
    u.display_name = "Ivan"
    u.username = "ivan"
    return u


@pytest.fixture
def client(fake_user):
    fake_db = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════
# GET /notifications
# ═══════════════════════════════════════════════════════════════════


def test_list_notifications_returns_empty_page(client, monkeypatch):
    monkeypatch.setattr(
        "app.features.notifications.router.NotificationService.list_for_user",
        AsyncMock(return_value=NotificationsPageOrm(items=[], next_cursor=None)),
    )
    r = client.get("/notifications")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["items"] == []
    assert body["data"]["next_cursor"] is None


def test_list_notifications_forwards_filters(client, monkeypatch):
    mock_list = AsyncMock(return_value=NotificationsPageOrm(items=[], next_cursor=None))
    monkeypatch.setattr(
        "app.features.notifications.router.NotificationService.list_for_user",
        mock_list,
    )
    r = client.get("/notifications?cursor=abc&limit=25&unread_only=true&category=tasks")
    assert r.status_code == 200
    assert mock_list.await_args.kwargs["cursor"] == "abc"
    assert mock_list.await_args.kwargs["limit"] == 25
    assert mock_list.await_args.kwargs["unread_only"] is True
    assert mock_list.await_args.kwargs["category"] == "tasks"


def test_list_notifications_rejects_limit_over_50(client):
    r = client.get("/notifications?limit=500")
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════
# POST /notifications/read
# ═══════════════════════════════════════════════════════════════════


def test_mark_read_returns_marked_count(client, monkeypatch):
    monkeypatch.setattr(
        "app.features.notifications.router.NotificationService.mark_read",
        AsyncMock(return_value=2),
    )
    r = client.post(
        "/notifications/read",
        json={"notification_ids": [str(uuid.uuid4()), str(uuid.uuid4())]},
    )
    assert r.status_code == 200
    assert r.json()["data"]["marked"] == 2


def test_mark_read_rejects_empty_list(client):
    r = client.post("/notifications/read", json={"notification_ids": []})
    assert r.status_code == 422


def test_mark_read_rejects_duplicates(client):
    dup = str(uuid.uuid4())
    r = client.post(
        "/notifications/read",
        json={"notification_ids": [dup, dup]},
    )
    assert r.status_code == 422


def test_mark_read_rejects_over_100(client):
    ids = [str(uuid.uuid4()) for _ in range(101)]
    r = client.post("/notifications/read", json={"notification_ids": ids})
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════
# DELETE /notifications/{id}
# ═══════════════════════════════════════════════════════════════════


def test_delete_notification_204(client, monkeypatch):
    monkeypatch.setattr(
        "app.features.notifications.router.NotificationService.soft_delete",
        AsyncMock(return_value=None),
    )
    r = client.delete(f"/notifications/{uuid.uuid4()}")
    assert r.status_code == 204


def test_delete_notification_404_propagates(client, monkeypatch):
    from app.core.errors.exceptions import ResourceNotFoundException

    monkeypatch.setattr(
        "app.features.notifications.router.NotificationService.soft_delete",
        AsyncMock(side_effect=ResourceNotFoundException("Notification")),
    )
    r = client.delete(f"/notifications/{uuid.uuid4()}")
    assert r.status_code == 404


def test_delete_notification_rejects_malformed_uuid(client):
    r = client.delete("/notifications/not-a-uuid")
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════
# GET /user/notification-preferences
# ═══════════════════════════════════════════════════════════════════


def test_get_preferences_returns_all_5_categories(client, monkeypatch):
    entries = [
        PreferenceEntry(category="tasks", channel="push"),
        PreferenceEntry(category="payments", channel="email"),
        PreferenceEntry(category="security", channel="email"),
        PreferenceEntry(category="digest", channel="none"),
        PreferenceEntry(category="product", channel="email"),
    ]
    monkeypatch.setattr(
        "app.features.notifications.router.NotificationPreferencesService.get_for_user",
        AsyncMock(return_value=entries),
    )
    r = client.get("/user/notification-preferences")
    assert r.status_code == 200
    prefs = r.json()["data"]["preferences"]
    assert len(prefs) == 5
    cats = {p["category"] for p in prefs}
    assert cats == {"tasks", "payments", "security", "digest", "product"}


# ═══════════════════════════════════════════════════════════════════
# PUT /user/notification-preferences
# ═══════════════════════════════════════════════════════════════════


def test_put_preferences_upserts_and_returns_updated(client, monkeypatch):
    entries_back = [
        PreferenceEntry(category="tasks", channel="both"),
        PreferenceEntry(category="payments", channel="email"),
        PreferenceEntry(category="security", channel="email"),
        PreferenceEntry(category="digest", channel="none"),
        PreferenceEntry(category="product", channel="email"),
    ]
    set_mock = AsyncMock(return_value=entries_back)
    monkeypatch.setattr(
        "app.features.notifications.router.NotificationPreferencesService.set_for_user",
        set_mock,
    )
    r = client.put(
        "/user/notification-preferences",
        json={"preferences": [{"category": "tasks", "channel": "both"}]},
    )
    assert r.status_code == 200
    assert set_mock.await_count == 1


def test_put_preferences_rejects_duplicate_category(client):
    r = client.put(
        "/user/notification-preferences",
        json={
            "preferences": [
                {"category": "tasks", "channel": "push"},
                {"category": "tasks", "channel": "email"},
            ]
        },
    )
    assert r.status_code == 422


def test_put_preferences_rejects_unknown_channel(client):
    r = client.put(
        "/user/notification-preferences",
        json={"preferences": [{"category": "tasks", "channel": "sms"}]},
    )
    assert r.status_code == 422


def test_put_preferences_rejects_unknown_category(client):
    r = client.put(
        "/user/notification-preferences",
        json={"preferences": [{"category": "marketing", "channel": "email"}]},
    )
    assert r.status_code == 422


def test_put_preferences_rejects_empty_list(client):
    r = client.put(
        "/user/notification-preferences",
        json={"preferences": []},
    )
    assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════
# POST /notifications/unsubscribe/{token}
# ═══════════════════════════════════════════════════════════════════


def test_unsubscribe_happy_path_sets_none(client, monkeypatch, fake_user):
    token = create_unsubscribe_token(fake_user.id, "digest")
    apply_mock = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.router.apply_unsubscribe",
        apply_mock,
    )
    monkeypatch.setattr(
        "app.features.notifications.router.check_ip_rate_limit",
        AsyncMock(return_value=None),
    )
    r = client.post(f"/notifications/unsubscribe/{token}")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["category"] == "digest"
    assert data["channel_after"] == "none"
    apply_mock.assert_awaited_once()


def test_unsubscribe_invalid_token_400(client, monkeypatch):
    monkeypatch.setattr(
        "app.features.notifications.router.check_ip_rate_limit",
        AsyncMock(return_value=None),
    )
    # 20 chars est le minimum imposé par Path(min_length=20), mais le
    # JWT garbage fera échouer la signature → 400 UNSUBSCRIBE_TOKEN_INVALID.
    r = client.post("/notifications/unsubscribe/" + "x" * 40)
    assert r.status_code == 400
    assert r.json()["code"] == "UNSUBSCRIBE_TOKEN_INVALID"


def test_unsubscribe_security_category_refused(client, monkeypatch, fake_user):
    token = create_unsubscribe_token(fake_user.id, "security")
    monkeypatch.setattr(
        "app.features.notifications.router.check_ip_rate_limit",
        AsyncMock(return_value=None),
    )
    r = client.post(f"/notifications/unsubscribe/{token}")
    assert r.status_code == 400
    assert r.json()["code"] == "UNSUBSCRIBE_SECURITY_REFUSED"


def test_unsubscribe_rate_limit_exceeded_429(client, monkeypatch, fake_user):
    from app.core.errors.exceptions import RateLimitIPException

    token = create_unsubscribe_token(fake_user.id, "tasks")
    monkeypatch.setattr(
        "app.features.notifications.router.check_ip_rate_limit",
        AsyncMock(side_effect=RateLimitIPException(retry_after=3600)),
    )
    r = client.post(f"/notifications/unsubscribe/{token}")
    assert r.status_code == 429


def test_unsubscribe_token_too_short_rejected_by_pydantic(client):
    r = client.post("/notifications/unsubscribe/shorty")
    assert r.status_code == 422
