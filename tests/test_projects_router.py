"""
Tests d'intégration — router `/projects` (Session C2).

On hit directement le router FastAPI via `TestClient`, avec :
- `get_current_user` surchargé pour injecter un user factice.
- `get_db` surchargé pour fournir une session inerte (service monkeypatché).
- `ProjectService.*` monkeypatché en `AsyncMock` — on vérifie le câblage
  router↔service + les bonnes transformations Pydantic et codes HTTP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    ProjectFilesQuotaExceededException,
    ProjectNameConflictException,
    ProjectQuotaExceededException,
    ResourceNotFoundException,
)
from app.features.auth.models import User
from app.features.chat import service as chat_service_module
from app.features.chat.service import ConversationService
from app.features.projects import service as projects_service_module
from app.features.projects.models import Project, ProjectFile
from app.features.projects.service import ProjectService
from app.main import app

# ══════════════════════════════════════════════════════════════
# Fixtures communes
# ══════════════════════════════════════════════════════════════

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_project(
    *,
    name: str = "École",
    icon_index: int = 0,
    color_index: int = 3,
    instructions: str | None = None,
    project_id: uuid.UUID | None = None,
) -> Project:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    p = Project(
        user_id=_FAKE_USER_ID,
        name=name,
        icon_index=icon_index,
        color_index=color_index,
        instructions=instructions,
    )
    p.id = project_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    p.created_at = now
    p.updated_at = now
    p.deleted_at = None
    return p


def _make_fake_view(project: Project, *, files: int = 0, convs: int = 0):
    return projects_service_module.ProjectView(
        project=project, file_count=files, conversation_count=convs
    )


def _make_fake_file(project_id: uuid.UUID) -> ProjectFile:
    now = datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC)
    f = ProjectFile(
        project_id=project_id,
        name="plan.pdf",
        file_type="pdf",
    )
    f.id = uuid.UUID("ffffffff-0000-4000-8000-000000000001")
    f.storage_key = None
    f.size_bytes = None
    f.mime_type = None
    f.uploaded_at = now
    f.created_at = now
    f.updated_at = now
    f.deleted_at = None
    return f


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
# 1. POST /projects — create
# ══════════════════════════════════════════════════════════════


def test_create_project_returns_201_with_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_fake_project(name="École", icon_index=2, color_index=5)
    monkeypatch.setattr(ProjectService, "create", AsyncMock(return_value=_make_fake_view(project)))

    response = client.post("/projects", json={"name": "École", "icon_index": 2, "color_index": 5})

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["name"] == "École"
    assert body["data"]["icon_index"] == 2
    assert body["data"]["color_index"] == 5
    assert body["data"]["file_count"] == 0
    assert body["data"]["conversation_count"] == 0


def test_create_project_returns_402_on_quota(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ProjectService,
        "create",
        AsyncMock(side_effect=ProjectQuotaExceededException(current=3, maximum=3, plan="free")),
    )

    response = client.post("/projects", json={"name": "Trop"})

    assert response.status_code == 402
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "PROJECT_QUOTA_EXCEEDED"
    assert body["data"] == {"current": 3, "max": 3, "plan": "free"}


def test_create_project_returns_409_on_name_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ProjectService,
        "create",
        AsyncMock(side_effect=ProjectNameConflictException()),
    )

    response = client.post("/projects", json={"name": "Déjà"})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "PROJECT_NAME_CONFLICT"


def test_create_project_rejects_icon_out_of_range(client: TestClient) -> None:
    response = client.post("/projects", json={"name": "OK", "icon_index": 25})
    assert response.status_code == 422


def test_create_project_rejects_color_out_of_range(client: TestClient) -> None:
    response = client.post("/projects", json={"name": "OK", "color_index": 18})
    assert response.status_code == 422


def test_create_project_rejects_empty_name(client: TestClient) -> None:
    response = client.post("/projects", json={"name": "   "})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 2. GET /projects — list
# ══════════════════════════════════════════════════════════════


def test_list_projects_forwards_q_and_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = projects_service_module.ProjectsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ProjectService, "list_for_user", mock)

    response = client.get("/projects?limit=10&q=scol")

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["q"] == "scol"
    assert kwargs["limit"] == 10


def test_list_projects_returns_page_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_fake_project(name="École")
    item = projects_service_module.ProjectListItemOrm(
        project=project,
        file_count=2,
        conversation_count=5,
        last_activity_at=None,
    )
    page = projects_service_module.ProjectsPageOrm(items=[item], next_cursor="cursor-abc")
    monkeypatch.setattr(ProjectService, "list_for_user", AsyncMock(return_value=page))

    response = client.get("/projects")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["next_cursor"] == "cursor-abc"
    assert len(body["data"]["items"]) == 1
    assert body["data"]["items"][0]["name"] == "École"
    assert body["data"]["items"][0]["file_count"] == 2
    assert body["data"]["items"][0]["conversation_count"] == 5


# ══════════════════════════════════════════════════════════════
# 3. GET /projects/{id} — 404 IDOR
# ══════════════════════════════════════════════════════════════


def test_get_project_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ProjectService,
        "get",
        AsyncMock(side_effect=ResourceNotFoundException("Projet")),
    )

    response = client.get(f"/projects/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "RESOURCE_NOT_FOUND"


def test_get_project_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _make_fake_project(name="Détail")
    monkeypatch.setattr(
        ProjectService,
        "get",
        AsyncMock(return_value=_make_fake_view(project, files=3, convs=7)),
    )

    response = client.get(f"/projects/{project.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["id"] == str(project.id)
    assert body["data"]["file_count"] == 3
    assert body["data"]["conversation_count"] == 7


# ══════════════════════════════════════════════════════════════
# 4. PATCH /projects/{id}
# ══════════════════════════════════════════════════════════════


def test_patch_project_happy_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _make_fake_project(name="Nouveau")
    monkeypatch.setattr(
        ProjectService,
        "update",
        AsyncMock(return_value=_make_fake_view(project)),
    )

    response = client.patch(f"/projects/{project.id}", json={"name": "Nouveau"})
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Nouveau"


def test_patch_project_409_on_conflict(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ProjectService,
        "update",
        AsyncMock(side_effect=ProjectNameConflictException()),
    )

    response = client.patch(f"/projects/{uuid.uuid4()}", json={"name": "Conflit"})
    assert response.status_code == 409


def test_patch_project_clear_instructions_flag_forwarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_fake_project(instructions=None)
    mock = AsyncMock(return_value=_make_fake_view(project))
    monkeypatch.setattr(ProjectService, "update", mock)

    response = client.patch(f"/projects/{project.id}", json={"clear_instructions": True})
    assert response.status_code == 200
    # `body` (2ème arg positionnel) contient clear_instructions=True.
    args, kwargs = mock.await_args.args, mock.await_args.kwargs
    body_arg = args[1] if len(args) > 1 else kwargs.get("body")
    assert body_arg is not None
    assert body_arg.clear_instructions is True


# ══════════════════════════════════════════════════════════════
# 5. DELETE /projects/{id}
# ══════════════════════════════════════════════════════════════


def test_delete_project_returns_204(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProjectService, "soft_delete", AsyncMock(return_value=None))

    response = client.delete(f"/projects/{uuid.uuid4()}")
    assert response.status_code == 204
    assert response.content == b""


# ══════════════════════════════════════════════════════════════
# 6. GET /projects/{id}/conversations — réutilise C1 FTS
# ══════════════════════════════════════════════════════════════


def test_list_project_conversations_forwards_project_id_to_chat_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_fake_project()
    monkeypatch.setattr(
        ProjectService,
        "_get_owned_project",
        AsyncMock(return_value=project),
    )
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get(f"/projects/{project.id}/conversations?q=intégrale")

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["project_id"] == project.id
    assert kwargs["q"] == "intégrale"


# ══════════════════════════════════════════════════════════════
# 7. POST /projects/{id}/files — create file metadata
# ══════════════════════════════════════════════════════════════


def test_create_file_returns_201(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _make_fake_project()
    pfile = _make_fake_file(project.id)
    monkeypatch.setattr(ProjectService, "add_file", AsyncMock(return_value=pfile))

    response = client.post(
        f"/projects/{project.id}/files",
        json={"name": "plan.pdf", "file_type": "pdf"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["name"] == "plan.pdf"
    assert body["data"]["file_type"] == "pdf"


def test_create_file_returns_402_on_quota(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ProjectService,
        "add_file",
        AsyncMock(
            side_effect=ProjectFilesQuotaExceededException(current=5, maximum=5, plan="free")
        ),
    )

    response = client.post(
        f"/projects/{uuid.uuid4()}/files",
        json={"name": "photo.png", "file_type": "image"},
    )
    assert response.status_code == 402
    body = response.json()
    assert body["code"] == "PROJECT_FILES_QUOTA_EXCEEDED"
    assert body["data"]["max"] == 5


def test_create_file_rejects_unknown_type(client: TestClient) -> None:
    response = client.post(
        f"/projects/{uuid.uuid4()}/files",
        json={"name": "x.zip", "file_type": "zip"},
    )
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 8. DELETE /projects/{id}/files/{file_id}
# ══════════════════════════════════════════════════════════════


def test_delete_file_returns_204(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProjectService, "remove_file", AsyncMock(return_value=None))

    response = client.delete(f"/projects/{uuid.uuid4()}/files/{uuid.uuid4()}")
    assert response.status_code == 204
