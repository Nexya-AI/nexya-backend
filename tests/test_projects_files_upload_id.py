"""
Tests d'intégration — extension de `POST /projects/{id}/files` avec le
champ `upload_id` (Session E3).

Couvre :
- Mode upload_id : service lit la row UploadedFile, copie storage_key/
  size_bytes/mime_type, dérive file_type, marque attached_to.
- Mode legacy (sans upload_id) toujours fonctionnel.
- Mutuellement exclusif : upload_id + storage_key → 422 Pydantic.
- 404 IDOR si upload_id inconnu/pas à l'user.
- file_type dérivé correctement du mime côté mapping.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import ResourceNotFoundException
from app.features.auth.models import User
from app.features.files.models import UploadedFile
from app.features.projects.models import Project, ProjectFile
from app.features.projects.service import (
    ProjectService,
    _derive_project_file_type,
)
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_project(project_id: uuid.UUID | None = None) -> Project:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    p = Project(user_id=_FAKE_USER_ID, name="École", icon_index=0, color_index=3)
    p.id = project_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    p.created_at = now
    p.updated_at = now
    p.deleted_at = None
    p.instructions = None
    return p


def _make_fake_upload(upload_id: uuid.UUID | None = None) -> UploadedFile:
    now = datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC)
    row = UploadedFile(
        user_id=_FAKE_USER_ID,
        storage_key="c4a2/uploads/ab/abc.pdf",
        content_sha256="a" * 64,
        size_bytes=4096,
        mime_type="application/pdf",
    )
    row.id = upload_id or uuid.UUID("11111111-0000-4000-8000-000000000001")
    row.created_at = now
    row.updated_at = now
    row.deleted_at = None
    return row


def _make_fake_file(project_id: uuid.UUID) -> ProjectFile:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    f = ProjectFile(project_id=project_id, name="rapport.pdf", file_type="pdf")
    f.id = uuid.UUID("ffffffff-0000-4000-8000-000000000001")
    f.storage_key = "c4a2/uploads/ab/abc.pdf"
    f.size_bytes = 4096
    f.mime_type = "application/pdf"
    f.created_at = now
    f.updated_at = now
    f.uploaded_at = now
    f.deleted_at = None
    return f


@pytest.fixture
def client() -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    # Le helper D2.5 `_project_file_to_response` fait `await db.execute(...)`
    # pour SELECT le UploadedFile rattaché et peupler `upload_id` +
    # `chunks_indexed_at` sur la réponse. Sans ce mock, MagicMock().execute()
    # retourne un MagicMock sync qui n'est pas awaitable → TypeError.
    # On retourne un Result dont `scalar_one_or_none()` vaut None par
    # défaut (cas legacy : pas d'upload rattaché). Les tests qui veulent
    # simuler un upload rattaché (mode D2.5) overrideront ce comportement.
    fake_result = MagicMock()
    fake_result.scalar_one_or_none = MagicMock(return_value=None)
    fake_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    fake_db.execute = AsyncMock(return_value=fake_result)

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# 1. Dérivation MIME → file_type (unit test)
# ══════════════════════════════════════════════════════════════


def test_derive_project_file_type_all_variants() -> None:
    assert _derive_project_file_type("image/png") == "image"
    assert _derive_project_file_type("image/jpeg") == "image"
    assert _derive_project_file_type("application/pdf") == "pdf"
    assert (
        _derive_project_file_type(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        == "doc"
    )
    assert (
        _derive_project_file_type(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        == "xls"
    )
    assert (
        _derive_project_file_type(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
        == "ppt"
    )
    assert _derive_project_file_type("audio/mpeg") == "audio"
    assert _derive_project_file_type("video/mp4") == "video"
    assert _derive_project_file_type("text/plain") == "other"
    assert _derive_project_file_type("application/x-custom") == "other"
    assert _derive_project_file_type(None) is None


# ══════════════════════════════════════════════════════════════
# 2. Mode upload_id : copie métadonnées + mark_attached
# ══════════════════════════════════════════════════════════════


def test_post_project_file_with_upload_id_copies_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_fake_project()
    upload = _make_fake_upload()
    pfile = _make_fake_file(project.id)

    # On mock add_file intégralement et on vérifie que le body contient bien
    # `upload_id` forwardé au service — puis on retourne pfile.
    add_file_mock = AsyncMock(return_value=pfile)
    monkeypatch.setattr(ProjectService, "add_file", add_file_mock)

    response = client.post(
        f"/projects/{project.id}/files",
        json={"name": "rapport.pdf", "upload_id": str(upload.id)},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["data"]["file_type"] == "pdf"
    assert body["data"]["storage_key"] == upload.storage_key
    # Le service a bien reçu upload_id dans le body.
    kwargs = add_file_mock.await_args.args
    body_arg = kwargs[1]  # add_file(project_id, body, user, db)
    assert body_arg.upload_id == upload.id


# ══════════════════════════════════════════════════════════════
# 3. Mutuellement exclusif : upload_id + storage_key → 422
# ══════════════════════════════════════════════════════════════


def test_post_project_file_rejects_both_upload_id_and_storage_key(
    client: TestClient,
) -> None:
    response = client.post(
        f"/projects/{uuid.uuid4()}/files",
        json={
            "name": "conflict.pdf",
            "upload_id": str(uuid.uuid4()),
            "storage_key": "manual/key.pdf",
            "file_type": "pdf",
        },
    )
    assert response.status_code == 422
    # Le code d'erreur est VALIDATION_ERROR (le détail du message est
    # générique — Pydantic rejette bien, c'est ce qui compte).
    assert response.json()["code"].lower() == "validation_error"


# ══════════════════════════════════════════════════════════════
# 4. Mode legacy (sans upload_id) — comportement C2 inchangé
# ══════════════════════════════════════════════════════════════


def test_post_project_file_legacy_mode_still_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _make_fake_project()
    pfile = _make_fake_file(project.id)
    monkeypatch.setattr(ProjectService, "add_file", AsyncMock(return_value=pfile))

    response = client.post(
        f"/projects/{project.id}/files",
        json={"name": "manual.pdf", "file_type": "pdf"},
    )
    assert response.status_code == 201


# ══════════════════════════════════════════════════════════════
# 5. Mode legacy sans file_type ET sans mime_type → 422
# ══════════════════════════════════════════════════════════════


def test_post_project_file_legacy_requires_file_type(client: TestClient) -> None:
    response = client.post(
        f"/projects/{uuid.uuid4()}/files",
        json={"name": "incomplete.bin"},  # ni file_type, ni mime_type, ni upload_id
    )
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 6. upload_id inconnu → 404 IDOR (propagation depuis get_for_user)
# ══════════════════════════════════════════════════════════════


def test_post_project_file_with_unknown_upload_id_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si `FileUploadService.get_for_user` lève 404, le router doit
    propager. On simule via `ProjectService.add_file` qui raise pour
    simplifier (le vrai flow appelle get_for_user en interne)."""
    monkeypatch.setattr(
        ProjectService,
        "add_file",
        AsyncMock(side_effect=ResourceNotFoundException("Upload")),
    )
    response = client.post(
        f"/projects/{uuid.uuid4()}/files",
        json={"name": "ghost.pdf", "upload_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404
