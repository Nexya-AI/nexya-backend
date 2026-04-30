"""N1 — Tests router POST /suggestions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import RateLimitAbuseException
from app.features.suggestions.models import UserSuggestion
from app.main import app


def _fake_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "user@nexya.ai"
    return user


@pytest.fixture
def fake_user():
    return _fake_user()


@pytest.fixture
def client(fake_user):
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: MagicMock(execute=AsyncMock())
    yield TestClient(app)
    app.dependency_overrides.clear()


def _mk_suggestion(user_id, type_="feature", body="test"):
    s = MagicMock(spec=UserSuggestion)
    s.id = uuid.uuid4()
    s.user_id = user_id
    s.suggestion_type = type_
    s.body = body
    s.processing_status = "open"
    s.created_at = datetime.now(UTC)
    return s


def test_post_suggestion_201(monkeypatch, client, fake_user):
    suggestion = _mk_suggestion(fake_user.id, type_="bug", body="Crash chat")
    monkeypatch.setattr(
        "app.features.suggestions.router.SuggestionService.submit",
        AsyncMock(return_value=suggestion),
    )
    response = client.post(
        "/suggestions",
        json={"suggestion_type": "bug", "body": "Crash chat"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["suggestion_type"] == "bug"
    assert body["data"]["processing_status"] == "open"


def test_post_suggestion_422_invalid_type(client):
    response = client.post(
        "/suggestions",
        json={"suggestion_type": "hack", "body": "x"},
    )
    assert response.status_code == 422


def test_post_suggestion_422_empty_body(client):
    response = client.post(
        "/suggestions",
        json={"suggestion_type": "bug", "body": ""},
    )
    assert response.status_code == 422


def test_post_suggestion_429_rate_limit(monkeypatch, client):
    async def _raise(*args, **kwargs):
        raise RateLimitAbuseException(retry_after=86400)

    monkeypatch.setattr("app.features.suggestions.router.SuggestionService.submit", _raise)
    response = client.post(
        "/suggestions",
        json={"suggestion_type": "feature", "body": "Mode sombre"},
    )
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "RATE_LIMIT_ABUSE"
