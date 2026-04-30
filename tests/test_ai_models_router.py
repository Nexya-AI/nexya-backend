"""N1 — Tests router /models."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.main import app


def _fake_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.is_active = True
    return user


@pytest.fixture
def client():
    fake_user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: MagicMock(execute=AsyncMock())
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_models_endpoint_200(client):
    response = client.get("/models")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "models" in body["data"]
    assert "experts_routing" in body["data"]


def test_models_endpoint_at_least_5_providers(client):
    response = client.get("/models")
    body = response.json()
    providers = {m["provider"] for m in body["data"]["models"]}
    # Dev mode = 5 providers chat (gemini réel + 4 mocks usurpants
    # openai/anthropic/qwen/openrouter) + image gemini-imagen
    assert len(providers) >= 5


def test_models_endpoint_cache_control_private(client):
    response = client.get("/models")
    cc = response.headers.get("Cache-Control") or response.headers.get("cache-control")
    assert "private" in cc
    # max-age default 300
    assert "max-age=" in cc


def test_models_endpoint_no_auth_returns_401():
    """Sans override, endpoint refuse."""
    app.dependency_overrides.clear()
    client = TestClient(app)
    response = client.get("/models")
    assert response.status_code in (401, 403)
