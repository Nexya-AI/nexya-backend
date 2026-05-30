"""Tests POST /code-projects/build-zip router (C4.6).

Pattern strict aligné `tests/features/chat/test_create_with_project_id.py` :
- FastAPI TestClient avec app.dependency_overrides
- Mock get_current_user + get_db
- Pas de Postgres ni Redis réels
- check_user_rate_limit monkeypatch no-op (économise Redis)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.code_projects.schemas import BuildZipResponse
from app.features.code_projects.service import CodeProjectService
from app.main import app


@pytest.fixture
def fake_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    return user


@pytest.fixture
def fake_db():
    return MagicMock()


@pytest.fixture
def client(fake_user, fake_db, monkeypatch):
    # Override Depends
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_db

    # Bypass Redis rate limit — patch dans le namespace du router
    # (l'import `from ... import check_user_rate_limit` du router crée
    # une référence locale au moment de l'import, patcher le module
    # source `rate_limiter` ne suffit pas — pattern aligné test_rgpd_router).
    monkeypatch.setattr(
        "app.features.code_projects.router.check_user_rate_limit",
        AsyncMock(return_value=None),
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


def _make_payload_dict(**overrides) -> dict:
    base = {
        "project_name": "Test API",
        "files": [
            {
                "filename": "main.py",
                "content": "from fastapi import FastAPI\napp = FastAPI()",
                "language": "python",
            },
            {
                "filename": "requirements.txt",
                "content": "fastapi==0.100.0",
                "language": "text",
            },
        ],
    }
    base.update(overrides)
    return base


class TestBuildZipRouter:
    """POST /code-projects/build-zip endpoint."""

    def test_happy_path(self, client, monkeypatch):
        from datetime import UTC, datetime, timedelta

        fake_response = BuildZipResponse(
            download_url="mock://test/key.zip",
            filename="Test API.zip",
            size_bytes=1024,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )

        monkeypatch.setattr(
            CodeProjectService,
            "build_zip",
            AsyncMock(return_value=fake_response),
        )

        response = client.post(
            "/code-projects/build-zip",
            json={"payload": _make_payload_dict()},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["download_url"] == "mock://test/key.zip"
        assert body["data"]["filename"] == "Test API.zip"
        assert body["data"]["size_bytes"] == 1024

    def test_validation_error_single_file(self, client):
        # Pydantic refuse < 2 fichiers (cap min).
        response = client.post(
            "/code-projects/build-zip",
            json={
                "payload": _make_payload_dict(
                    files=[
                        {
                            "filename": "main.py",
                            "content": "print('hi')",
                            "language": "python",
                        },
                    ]
                )
            },
        )
        assert response.status_code == 422

    def test_validation_error_filename_path_traversal(self, client):
        response = client.post(
            "/code-projects/build-zip",
            json={
                "payload": _make_payload_dict(
                    files=[
                        {
                            "filename": "../etc/passwd.txt",
                            "content": "malicious",
                            "language": "text",
                        },
                        {
                            "filename": "main.py",
                            "content": "print('hi')",
                            "language": "python",
                        },
                    ]
                )
            },
        )
        assert response.status_code == 422

    def test_validation_error_duplicate_filenames(self, client):
        response = client.post(
            "/code-projects/build-zip",
            json={
                "payload": _make_payload_dict(
                    files=[
                        {
                            "filename": "main.py",
                            "content": "print(1)",
                            "language": "python",
                        },
                        {
                            "filename": "main.py",
                            "content": "print(2)",
                            "language": "python",
                        },
                    ]
                )
            },
        )
        assert response.status_code == 422

    def test_zip_size_exceeded_raises_422(self, client, monkeypatch):
        # Service lève ValueError → router map en HTTPException 422.
        async def raises_value_error(**kwargs):
            raise ValueError("Le .zip généré dépasse le cap dur de 50 MB")

        monkeypatch.setattr(
            CodeProjectService, "build_zip", AsyncMock(side_effect=raises_value_error)
        )

        response = client.post(
            "/code-projects/build-zip",
            json={"payload": _make_payload_dict()},
        )
        assert response.status_code == 422

    def test_missing_payload_field(self, client):
        # Body sans `payload` → 422 Pydantic.
        response = client.post("/code-projects/build-zip", json={})
        assert response.status_code == 422

    def test_files_min_length_enforced(self, client):
        # 0 fichier → 422.
        response = client.post(
            "/code-projects/build-zip",
            json={"payload": _make_payload_dict(files=[])},
        )
        assert response.status_code == 422

    def test_project_name_required(self, client):
        # Pas de project_name → 422.
        payload = _make_payload_dict()
        del payload["project_name"]
        response = client.post(
            "/code-projects/build-zip",
            json={"payload": payload},
        )
        assert response.status_code == 422


class TestBuildZipAuthGuard:
    """Endpoint protégé par get_current_user (réutilise A1)."""

    def test_endpoint_mounted_in_app(self):
        # Vérifie que la route est bien enregistrée dans l'app.
        paths = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/code-projects/build-zip" in paths
