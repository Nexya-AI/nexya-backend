"""N1 — Tests router feedback chat (POST + DELETE)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import ResourceNotFoundException
from app.features.feedback.models import MessageFeedback
from app.main import app


def _fake_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "user@nexya.ai"
    user.is_active = True
    return user


@pytest.fixture
def fake_user():
    return _fake_user()


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def client(fake_user, fake_db):
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _mk_feedback(user_id, message_id, rating="like", comment=None):
    row = MagicMock(spec=MessageFeedback)
    row.id = uuid.uuid4()
    row.user_id = user_id
    row.message_id = message_id
    row.rating = rating
    row.comment = comment
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def test_post_feedback_201(monkeypatch, client, fake_user):
    msg_id = uuid.uuid4()
    feedback = _mk_feedback(fake_user.id, msg_id, rating="like")
    monkeypatch.setattr(
        "app.features.chat.router.FeedbackService.record_feedback",
        AsyncMock(return_value=feedback),
    )
    response = client.post(
        f"/chat/messages/{msg_id}/feedback",
        json={"rating": "like"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["rating"] == "like"


def test_post_feedback_with_comment(monkeypatch, client, fake_user):
    msg_id = uuid.uuid4()
    feedback = _mk_feedback(fake_user.id, msg_id, rating="dislike", comment="trop générique")
    monkeypatch.setattr(
        "app.features.chat.router.FeedbackService.record_feedback",
        AsyncMock(return_value=feedback),
    )
    response = client.post(
        f"/chat/messages/{msg_id}/feedback",
        json={"rating": "dislike", "comment": "trop générique"},
    )
    assert response.status_code == 201
    assert response.json()["data"]["comment"] == "trop générique"


def test_post_feedback_404_idor(monkeypatch, client):
    msg_id = uuid.uuid4()

    async def _raise(*args, **kwargs):
        raise ResourceNotFoundException("Message")

    monkeypatch.setattr("app.features.chat.router.FeedbackService.record_feedback", _raise)
    response = client.post(
        f"/chat/messages/{msg_id}/feedback",
        json={"rating": "like"},
    )
    assert response.status_code == 404


def test_post_feedback_422_invalid_rating(client):
    msg_id = uuid.uuid4()
    response = client.post(
        f"/chat/messages/{msg_id}/feedback",
        json={"rating": "thumbs_up"},
    )
    assert response.status_code == 422


def test_post_feedback_422_comment_too_long(client):
    msg_id = uuid.uuid4()
    response = client.post(
        f"/chat/messages/{msg_id}/feedback",
        json={"rating": "like", "comment": "x" * 1001},
    )
    assert response.status_code == 422


def test_post_feedback_422_invalid_uuid(client):
    response = client.post(
        "/chat/messages/not-a-uuid/feedback",
        json={"rating": "like"},
    )
    assert response.status_code == 422


def test_delete_feedback_204(monkeypatch, client):
    msg_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.features.chat.router.FeedbackService.delete_feedback",
        AsyncMock(return_value=None),
    )
    response = client.delete(f"/chat/messages/{msg_id}/feedback")
    assert response.status_code == 204


def test_delete_feedback_idempotent_no_raise(monkeypatch, client):
    """Pas de 404 quand pas de row — anti-énumération."""
    msg_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.features.chat.router.FeedbackService.delete_feedback",
        AsyncMock(return_value=None),
    )
    response = client.delete(f"/chat/messages/{msg_id}/feedback")
    assert response.status_code == 204
