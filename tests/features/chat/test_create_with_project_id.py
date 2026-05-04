"""Tests D3 — extension `project_id` sur `ConversationCreate` +
`ChatStreamRequest` + `ConversationService.{create, ensure_conversation_for_stream}`.

7 scénarios mock-first (pattern aligné `tests/features/files/`) :

1. Pydantic `ConversationCreate` : project_id UUID accepté, null accepté,
   string non-UUID rejeté en 422 (ValidationError).
2. Pydantic `ChatStreamRequest` : idem avec project_id.
3. Service `create` happy path avec project_id : `_get_owned_project`
   mocké renvoie un projet valide, INSERT contient bien project_id, commit
   appelé, log `chat.conversation.created` inclut project_id.
4. Service `create` 404 IDOR : `_get_owned_project` lève
   `ResourceNotFoundException`, le service propage sans toucher DB
   (zéro INSERT, zéro commit).
5. Service `create` sans project_id : comportement legacy strictement
   préservé (project_id=None sur la row, log sans project_id).
6. Service `ensure_conversation_for_stream` happy path nouvelle conv
   avec project_id : ownership check passe, création conv attachée.
7. Service `ensure_conversation_for_stream` ignore silencieux : si
   `conversation_id != None` ET `project_id != None`, le service charge
   simplement la conv existante via `_get_owned_conversation` SANS
   appeler `_get_owned_project`, et émet un log debug sans raise.

Pattern test : `AsyncMock(spec=AsyncSession)` + `MagicMock(spec=User)` +
monkeypatch `ProjectService._get_owned_project` pour court-circuiter la
DB côté backend C2. Aucun Postgres réel requis (tests sub-seconde).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.auth.models import User
from app.features.chat.models import Conversation
from app.features.chat.schemas import ChatStreamRequest, ConversationCreate
from app.features.chat.service import ConversationService

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _make_user(user_id: uuid.UUID | None = None) -> MagicMock:
    """Fake `User` ORM avec un id stable (UUID v4 si non fourni)."""
    user = MagicMock(spec=User)
    user.id = user_id or uuid.uuid4()
    return user


def _make_db(*, refresh_attrs: dict | None = None) -> MagicMock:
    """Fake `AsyncSession` qui accepte add/commit/refresh sans I/O réelle.

    `refresh_attrs` : si fourni, le `refresh(obj)` mocké pose ces
    attributs sur l'objet ORM (utile pour simuler `Conversation.id`
    rempli par Postgres après commit).
    """
    db = MagicMock(spec=AsyncSession)
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def _refresh(obj):
        if refresh_attrs is not None:
            for k, v in refresh_attrs.items():
                setattr(obj, k, v)

    db.refresh = AsyncMock(side_effect=_refresh)
    db.execute = AsyncMock()  # pour _get_owned_conversation côté ensure
    return db


# ════════════════════════════════════════════════════════════════════
# Test 1 — Pydantic ConversationCreate avec project_id
# ════════════════════════════════════════════════════════════════════


class TestConversationCreateSchema:
    """Validation Pydantic stricte sur le nouveau champ `project_id`."""

    def test_accepts_valid_uuid(self) -> None:
        pid = uuid.uuid4()
        body = ConversationCreate(project_id=pid)
        assert body.project_id == pid

    def test_accepts_uuid_string_form(self) -> None:
        pid = uuid.uuid4()
        body = ConversationCreate(project_id=str(pid))
        assert body.project_id == pid

    def test_accepts_null(self) -> None:
        body = ConversationCreate()
        assert body.project_id is None

    def test_rejects_invalid_format(self) -> None:
        with pytest.raises(ValidationError):
            ConversationCreate(project_id="not-a-uuid")


# ════════════════════════════════════════════════════════════════════
# Test 2 — Pydantic ChatStreamRequest avec project_id
# ════════════════════════════════════════════════════════════════════


class TestChatStreamRequestSchema:
    """Validation Pydantic sur `ChatStreamRequest.project_id`."""

    def test_accepts_valid_uuid(self) -> None:
        pid = uuid.uuid4()
        body = ChatStreamRequest(message="hello", project_id=pid)
        assert body.project_id == pid

    def test_accepts_null(self) -> None:
        body = ChatStreamRequest(message="hello")
        assert body.project_id is None

    def test_rejects_invalid_format(self) -> None:
        with pytest.raises(ValidationError):
            ChatStreamRequest(message="hello", project_id="garbage")


# ════════════════════════════════════════════════════════════════════
# Test 3 — Service create happy path avec project_id
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_with_project_id_happy_path(monkeypatch) -> None:
    """`create` avec project_id : ownership check passe, INSERT contient
    bien project_id, commit appelé une fois."""
    user = _make_user()
    pid = uuid.uuid4()

    fake_project = MagicMock()
    fake_project.id = pid

    # Patch ProjectService._get_owned_project pour court-circuiter la DB.
    from app.features.projects import service as projects_service

    get_owned = AsyncMock(return_value=fake_project)
    monkeypatch.setattr(projects_service.ProjectService, "_get_owned_project", get_owned)

    db = _make_db(refresh_attrs={"id": uuid.uuid4()})
    body = ConversationCreate(title="École", project_id=pid)

    conv = await ConversationService.create(body, user, db)

    # Ownership check appelé exactement 1 fois avec les bons args.
    get_owned.assert_awaited_once_with(pid, user.id, db)
    # INSERT effectué : add appelé une fois avec une Conversation portant
    # le project_id fourni.
    assert db.add.call_count == 1
    inserted = db.add.call_args.args[0]
    assert isinstance(inserted, Conversation)
    assert inserted.user_id == user.id
    assert inserted.project_id == pid
    assert inserted.title == "École"
    # Commit appelé exactement 1 fois.
    db.commit.assert_awaited_once()
    # La conv retournée est bien celle qu'on a insérée.
    assert conv is inserted


# ════════════════════════════════════════════════════════════════════
# Test 4 — Service create 404 IDOR (projet pas owner)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_with_project_id_idor_safe(monkeypatch) -> None:
    """`create` quand `_get_owned_project` lève `ResourceNotFoundException` :
    le service propage l'exception sans toucher la DB (zéro INSERT, zéro
    commit). Aligne le pattern 404 IDOR-safe (anti-énumération UUID).
    """
    user = _make_user()
    pid = uuid.uuid4()

    from app.features.projects import service as projects_service

    get_owned = AsyncMock(side_effect=ResourceNotFoundException("Projet"))
    monkeypatch.setattr(projects_service.ProjectService, "_get_owned_project", get_owned)

    db = _make_db()
    body = ConversationCreate(project_id=pid)

    with pytest.raises(ResourceNotFoundException):
        await ConversationService.create(body, user, db)

    # Ownership check appelé, mais zéro écriture DB.
    get_owned.assert_awaited_once_with(pid, user.id, db)
    db.add.assert_not_called()
    db.commit.assert_not_called()


# ════════════════════════════════════════════════════════════════════
# Test 5 — Service create sans project_id (legacy préservé)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_without_project_id_preserves_legacy(monkeypatch) -> None:
    """`create` sans project_id : aucun appel à `_get_owned_project`,
    comportement legacy strictement préservé (la row est INSERT avec
    project_id=None)."""
    user = _make_user()

    from app.features.projects import service as projects_service

    get_owned = AsyncMock()
    monkeypatch.setattr(projects_service.ProjectService, "_get_owned_project", get_owned)

    db = _make_db(refresh_attrs={"id": uuid.uuid4()})
    body = ConversationCreate(title="Sans projet")

    conv = await ConversationService.create(body, user, db)

    # Ownership check JAMAIS appelé.
    get_owned.assert_not_called()
    # INSERT avec project_id=None.
    assert db.add.call_count == 1
    inserted = db.add.call_args.args[0]
    assert inserted.project_id is None
    assert inserted.title == "Sans projet"
    db.commit.assert_awaited_once()
    assert conv is inserted


# ════════════════════════════════════════════════════════════════════
# Test 6 — ensure_conversation_for_stream : nouvelle conv avec project_id
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ensure_for_stream_new_conv_with_project_id(monkeypatch) -> None:
    """`ensure_conversation_for_stream(conversation_id=None, project_id=X)` :
    ownership check passe, création de la conv attachée au projet."""
    user = _make_user()
    pid = uuid.uuid4()

    from app.features.projects import service as projects_service

    fake_project = MagicMock()
    fake_project.id = pid
    get_owned = AsyncMock(return_value=fake_project)
    monkeypatch.setattr(projects_service.ProjectService, "_get_owned_project", get_owned)

    db = _make_db(refresh_attrs={"id": uuid.uuid4()})

    conv = await ConversationService.ensure_conversation_for_stream(
        None,
        user,
        db,
        expert_id_hint="cooking",
        project_id=pid,
    )

    get_owned.assert_awaited_once_with(pid, user.id, db)
    assert db.add.call_count == 1
    inserted = db.add.call_args.args[0]
    assert isinstance(inserted, Conversation)
    assert inserted.project_id == pid
    assert inserted.expert_id == "cooking"
    db.commit.assert_awaited_once()
    assert conv is inserted


# ════════════════════════════════════════════════════════════════════
# Test 7 — ensure_conversation_for_stream : ignore silencieux
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ensure_for_stream_existing_conv_ignores_project_id(
    monkeypatch,
) -> None:
    """`ensure_conversation_for_stream(conversation_id=X, project_id=Y)` :
    le service ignore silencieusement project_id (pas d'appel à
    `_get_owned_project`), charge simplement la conv existante via
    `_get_owned_conversation`. Pas de raise, pas d'INSERT, pas de commit
    (le helper retourne juste la conv chargée)."""
    user = _make_user()
    cid = uuid.uuid4()
    pid = uuid.uuid4()

    from app.features.projects import service as projects_service

    get_owned_project = AsyncMock()
    monkeypatch.setattr(
        projects_service.ProjectService,
        "_get_owned_project",
        get_owned_project,
    )

    # Patch `_get_owned_conversation` pour court-circuiter la DB et
    # retourner une conv synthétique.
    fake_conv = MagicMock(spec=Conversation)
    fake_conv.id = cid
    fake_conv.user_id = user.id
    fake_conv.project_id = None
    get_owned_conv = AsyncMock(return_value=fake_conv)
    monkeypatch.setattr(
        ConversationService,
        "_get_owned_conversation",
        get_owned_conv,
    )

    db = _make_db()

    conv = await ConversationService.ensure_conversation_for_stream(
        cid,
        user,
        db,
        expert_id_hint=None,
        project_id=pid,
    )

    # Ownership PROJET jamais appelé (le mode existing l'ignore).
    get_owned_project.assert_not_called()
    # Ownership CONV appelé pour vérifier que la conv appartient à l'user.
    get_owned_conv.assert_awaited_once_with(cid, user.id, db)
    # Aucun INSERT / commit dans ce chemin (on retourne la conv chargée).
    db.add.assert_not_called()
    db.commit.assert_not_called()
    # La conv retournée est bien celle chargée.
    assert conv is fake_conv
