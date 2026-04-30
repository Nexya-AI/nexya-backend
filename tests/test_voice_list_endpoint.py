"""N1 — Tests GET /voice/list catalogue NEXYA branded."""

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
    user.email = "free@nexya.ai"
    user.is_active = True
    user.is_pro = False  # explicitement Free pour tester pas de gate Pro
    return user


@pytest.fixture
def client():
    fake_user = _fake_user()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: MagicMock(execute=AsyncMock())
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_voice_list_returns_6_voices(client):
    response = client.get("/voice/list")
    assert response.status_code == 200
    body = response.json()
    voices = body["data"]["voices"]
    ids = {v["id"] for v in voices}
    assert ids == {
        "aurora",
        "memora",
        "soleil",
        "sagesse",
        "eron",
        "nyanga",
    }


def test_voice_list_each_voice_has_required_fields(client):
    response = client.get("/voice/list")
    voices = response.json()["data"]["voices"]
    for voice in voices:
        assert "id" in voice
        assert "name" in voice
        assert "personality" in voice
        assert "tone" in voice
        assert "language" in voice
        assert voice["tone"] in ("deep", "medium", "high")


def test_voice_list_cache_control_header(client):
    response = client.get("/voice/list")
    assert "cache-control" in {k.lower() for k in response.headers}
    cc = response.headers.get("Cache-Control") or response.headers.get("cache-control")
    assert "public" in cc
    assert "3600" in cc


def test_voice_list_free_user_can_access(client):
    """Free user accède au catalogue (PAS Pro-only)."""
    response = client.get("/voice/list")
    # Pas 403 PLAN_REQUIRED — c'est le test critique
    assert response.status_code != 403
    assert response.status_code == 200


def test_voice_list_no_auth_returns_401():
    """Sans token, l'endpoint refuse."""
    app.dependency_overrides.clear()  # ne pas overrider get_current_user
    client = TestClient(app)
    response = client.get("/voice/list")
    # 401 ou 403 selon Bearer header missing
    assert response.status_code in (401, 403)
