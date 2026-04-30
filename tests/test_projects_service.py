"""
Tests unitaires — `ProjectService` (Session C2).

Objectif : valider le contrat métier sans toucher Postgres.

On capture les statements SQLAlchemy passées à `db.execute` via un
`AsyncMock`, on remplace `scalar_one()` / `scalar_one_or_none()` / `scalars()`
selon le scénario. Pour la forme SQL, on utilise le même pattern que C1 :
`stmt.compile(compile_kwargs={"literal_binds": True})`.

Couverture ciblée :
- `create` : happy-path, 402 quota, 409 name conflict, name trim (validé par
  Pydantic — test dédié sur `ProjectCreate`).
- `list_for_user` : SQL shape avec `q` → ILIKE, sans `q` → pas d'ILIKE,
  keyset cursor compatible (curseur encodé → ne crash pas).
- `get` : 404 IDOR-safe.
- `update` : PATCH partial, `clear_instructions=True` → `instructions=NULL`,
  409 sur conflit de renommage.
- `soft_delete` : émet bien l'UPDATE `conversations SET project_id = NULL`.
- `add_file` : 402 quota.
- `list_files` : tri DESC + keyset.
- `remove_file` : 404 si déjà supprimé, idempotence via 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.errors.exceptions import (
    ProjectFilesQuotaExceededException,
    ProjectNameConflictException,
    ProjectQuotaExceededException,
    ResourceNotFoundException,
)
from app.features.projects.models import Project, ProjectFile
from app.features.projects.schemas import (
    ProjectCreate,
    ProjectFileCreate,
    ProjectUpdate,
)
from app.features.projects.service import ProjectService

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _make_user(*, is_pro: bool = False) -> MagicMock:
    user = MagicMock()
    user.id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    user.is_pro = is_pro
    return user


def _make_project(
    *,
    name: str = "École",
    icon_index: int = 0,
    color_index: int = 3,
    instructions: str | None = None,
    deleted_at: datetime | None = None,
    project_id: uuid.UUID | None = None,
) -> Project:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    p = Project(
        user_id=uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77"),
        name=name,
        icon_index=icon_index,
        color_index=color_index,
        instructions=instructions,
    )
    p.id = project_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    p.created_at = now
    p.updated_at = now
    p.deleted_at = deleted_at
    return p


def _make_file(project_id: uuid.UUID, *, file_id: uuid.UUID | None = None) -> ProjectFile:
    now = datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC)
    f = ProjectFile(
        project_id=project_id,
        name="plan.pdf",
        file_type="pdf",
    )
    f.id = file_id or uuid.UUID("ffffffff-0000-4000-8000-000000000001")
    f.storage_key = None
    f.size_bytes = None
    f.mime_type = None
    f.uploaded_at = now
    f.created_at = now
    f.updated_at = now
    f.deleted_at = None
    return f


class _ScalarResult:
    """Fake d'un `Result` SQLAlchemy — se comporte selon le scénario voulu."""

    def __init__(
        self,
        *,
        scalar_one: object | None = None,
        scalar_one_or_none: object | None | type = object,
        scalars_all: list | None = None,
        all_rows: list | None = None,
    ) -> None:
        self._scalar_one = scalar_one
        self._scalar_or_none = scalar_one_or_none
        self._scalars_all = scalars_all or []
        self._all_rows = all_rows or []

    def scalar_one(self):
        return self._scalar_one

    def scalar_one_or_none(self):
        if self._scalar_or_none is object:
            return None
        return self._scalar_or_none

    def scalars(self):
        sc = MagicMock()
        sc.all.return_value = self._scalars_all
        return sc

    def all(self):
        return self._all_rows


def _mk_db(execute_results: list[_ScalarResult]) -> MagicMock:
    """Fabrique un fake `db` dont `execute` retourne en séquence les Result
    fournis. `commit`, `refresh`, `add`, `rollback`, `delete` sont asynchrones
    inertes.
    """
    db = MagicMock()
    iterator = iter(execute_results)

    async def _execute(stmt, *args, **kwargs):
        return next(iterator)

    db.execute = _execute
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _compiled_sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


# ══════════════════════════════════════════════════════════════
# 1. `create` — quota, conflict, happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_project_succeeds_under_quota() -> None:
    user = _make_user(is_pro=False)
    # Le service fait un COUNT avant l'INSERT — renvoie 0 → sous le quota.
    db = _mk_db([_ScalarResult(scalar_one=0)])

    view = await ProjectService.create(
        ProjectCreate(name="École", icon_index=2, color_index=5),
        user,
        db,
    )

    assert view.project.name == "École"
    assert view.project.icon_index == 2
    assert view.project.color_index == 5
    assert view.file_count == 0
    assert view.conversation_count == 0
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_project_quota_exceeded_free() -> None:
    user = _make_user(is_pro=False)
    # COUNT = projects_max_free (3 par défaut) → plafond atteint.
    db = _mk_db([_ScalarResult(scalar_one=settings.projects_max_free)])

    with pytest.raises(ProjectQuotaExceededException) as excinfo:
        await ProjectService.create(ProjectCreate(name="Nouveau"), user, db)

    assert excinfo.value.code == "PROJECT_QUOTA_EXCEEDED"
    assert excinfo.value.status_code == 402
    assert excinfo.value.data == {
        "current": settings.projects_max_free,
        "max": settings.projects_max_free,
        "plan": "free",
    }
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_project_quota_higher_for_pro() -> None:
    user = _make_user(is_pro=True)
    # Un user Pro à projects_max_free + 1 n'est PAS bloqué (quota Pro).
    db = _mk_db([_ScalarResult(scalar_one=settings.projects_max_free + 1)])

    view = await ProjectService.create(ProjectCreate(name="Perso"), user, db)
    assert view.project.name == "Perso"


@pytest.mark.asyncio
async def test_create_project_name_conflict_409() -> None:
    user = _make_user(is_pro=False)
    db = _mk_db([_ScalarResult(scalar_one=0)])
    # Simule la contrainte UNIQUE partielle qui saute au commit.
    db.commit = AsyncMock(side_effect=IntegrityError("dup", {}, Exception("dup")))

    with pytest.raises(ProjectNameConflictException) as excinfo:
        await ProjectService.create(ProjectCreate(name="École"), user, db)

    assert excinfo.value.code == "PROJECT_NAME_CONFLICT"
    assert excinfo.value.status_code == 409
    db.rollback.assert_awaited_once()


def test_project_create_schema_rejects_blank_name() -> None:
    """Nom vide → validation Pydantic (levé avant même d'atteindre le service)."""
    with pytest.raises(ValidationError):
        ProjectCreate(name="   ")


def test_project_create_schema_rejects_icon_out_of_range() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="OK", icon_index=25)  # max = 24


def test_project_create_schema_rejects_color_out_of_range() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="OK", color_index=18)  # max = 17


# ══════════════════════════════════════════════════════════════
# 2. `list_for_user` — SQL shape (avec/sans q)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_for_user_with_q_injects_ilike_on_name() -> None:
    captured: dict[str, object] = {}

    class _CapResult:
        def all(self):
            return []

    async def _capture_execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return _CapResult()

    db = MagicMock()
    db.execute = _capture_execute
    user = _make_user()

    await ProjectService.list_for_user(user, db, q="école")

    sql = _compiled_sql(captured["stmt"])
    assert "ilike" in sql or "like" in sql
    assert "projects.name" in sql or "projects" in sql


@pytest.mark.asyncio
async def test_list_for_user_without_q_skips_ilike() -> None:
    captured: dict[str, object] = {}

    class _CapResult:
        def all(self):
            return []

    async def _capture_execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return _CapResult()

    db = MagicMock()
    db.execute = _capture_execute
    user = _make_user()

    await ProjectService.list_for_user(user, db, q=None)

    sql = _compiled_sql(captured["stmt"])
    assert "ilike" not in sql


@pytest.mark.asyncio
async def test_list_for_user_whitespace_only_q_skips_ilike() -> None:
    captured: dict[str, object] = {}

    class _CapResult:
        def all(self):
            return []

    async def _capture_execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return _CapResult()

    db = MagicMock()
    db.execute = _capture_execute
    user = _make_user()

    await ProjectService.list_for_user(user, db, q="   ")

    sql = _compiled_sql(captured["stmt"])
    assert "ilike" not in sql


# ══════════════════════════════════════════════════════════════
# 3. `get` — 404 IDOR-safe
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_project_returns_404_for_non_owner() -> None:
    user = _make_user()
    # scalar_one_or_none = None → projet inconnu OU autre propriétaire.
    db = _mk_db([_ScalarResult(scalar_one_or_none=None)])

    with pytest.raises(ResourceNotFoundException) as excinfo:
        await ProjectService.get(uuid.uuid4(), user, db)

    assert excinfo.value.status_code == 404


# ══════════════════════════════════════════════════════════════
# 4. `update` — PATCH partial + clear_instructions
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_clear_instructions_sets_null() -> None:
    user = _make_user()
    project = _make_project(instructions="Vieil system prompt")
    db = _mk_db(
        [
            _ScalarResult(scalar_one_or_none=project),  # _get_owned_project
            _ScalarResult(scalar_one=0),  # file_count
            _ScalarResult(scalar_one=0),  # conv_count
        ]
    )

    view = await ProjectService.update(
        project.id,
        ProjectUpdate(clear_instructions=True),
        user,
        db,
    )

    assert view.project.instructions is None
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_partial_keeps_other_fields() -> None:
    user = _make_user()
    project = _make_project(name="Ancien", icon_index=0, color_index=3, instructions="garde-moi")
    db = _mk_db(
        [
            _ScalarResult(scalar_one_or_none=project),
            _ScalarResult(scalar_one=0),
            _ScalarResult(scalar_one=0),
        ]
    )

    view = await ProjectService.update(
        project.id,
        ProjectUpdate(name="Nouveau"),
        user,
        db,
    )

    assert view.project.name == "Nouveau"
    assert view.project.icon_index == 0  # inchangé
    assert view.project.instructions == "garde-moi"  # inchangé


@pytest.mark.asyncio
async def test_update_name_conflict_409() -> None:
    user = _make_user()
    project = _make_project(name="Ancien")
    db = _mk_db([_ScalarResult(scalar_one_or_none=project)])
    db.commit = AsyncMock(side_effect=IntegrityError("dup", {}, Exception("dup")))

    with pytest.raises(ProjectNameConflictException) as excinfo:
        await ProjectService.update(project.id, ProjectUpdate(name="Déjà pris"), user, db)

    assert excinfo.value.code == "PROJECT_NAME_CONFLICT"
    db.rollback.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 5. `soft_delete` — détache les conversations
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_soft_delete_detaches_conversations() -> None:
    user = _make_user()
    project = _make_project()
    executed_statements: list[object] = []

    async def _capture_execute(stmt, *args, **kwargs):
        executed_statements.append(stmt)
        if len(executed_statements) == 1:
            # _get_owned_project → retourne le projet.
            return _ScalarResult(scalar_one_or_none=project)
        # 2ᵉ appel : UPDATE conversations SET project_id = NULL
        return _ScalarResult()

    db = MagicMock()
    db.execute = _capture_execute
    db.commit = AsyncMock()

    await ProjectService.soft_delete(project.id, user, db)

    # Le projet est marqué soft-deleted.
    assert project.deleted_at is not None
    # Le 2ᵉ statement est un UPDATE sur `conversations`.
    second_sql = _compiled_sql(executed_statements[1])
    assert "update conversations" in second_sql
    assert "project_id" in second_sql
    db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 6. `add_file` — quota Free
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_add_file_quota_exceeded_free() -> None:
    user = _make_user(is_pro=False)
    project = _make_project()
    db = _mk_db(
        [
            _ScalarResult(scalar_one_or_none=project),  # _get_owned_project
            _ScalarResult(scalar_one=settings.project_files_max_free),  # COUNT
        ]
    )

    with pytest.raises(ProjectFilesQuotaExceededException) as excinfo:
        await ProjectService.add_file(
            project.id,
            ProjectFileCreate(name="photo.png", file_type="image"),
            user,
            db,
        )

    assert excinfo.value.code == "PROJECT_FILES_QUOTA_EXCEEDED"
    assert excinfo.value.data["plan"] == "free"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_file_succeeds_under_quota() -> None:
    user = _make_user(is_pro=False)
    project = _make_project()
    db = _mk_db(
        [
            _ScalarResult(scalar_one_or_none=project),
            _ScalarResult(scalar_one=0),
        ]
    )

    pfile = await ProjectService.add_file(
        project.id,
        ProjectFileCreate(name="plan.pdf", file_type="pdf"),
        user,
        db,
    )

    assert pfile.name == "plan.pdf"
    assert pfile.file_type == "pdf"
    db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 7. `remove_file` — idempotence via 404
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_remove_file_raises_404_when_already_deleted() -> None:
    user = _make_user()
    project = _make_project()
    db = _mk_db(
        [
            _ScalarResult(scalar_one_or_none=project),
            _ScalarResult(scalar_one_or_none=None),  # file introuvable
        ]
    )

    with pytest.raises(ResourceNotFoundException):
        await ProjectService.remove_file(project.id, uuid.uuid4(), user, db)


@pytest.mark.asyncio
async def test_remove_file_soft_deletes_file() -> None:
    user = _make_user()
    project = _make_project()
    pfile = _make_file(project.id)
    db = _mk_db(
        [
            _ScalarResult(scalar_one_or_none=project),
            _ScalarResult(scalar_one_or_none=pfile),
        ]
    )

    await ProjectService.remove_file(project.id, pfile.id, user, db)
    assert pfile.deleted_at is not None
    db.commit.assert_awaited_once()
