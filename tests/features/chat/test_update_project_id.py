"""Tests Add-to-project lot — extension `project_id` / `clear_project_id`
sur `ConversationUpdate` + `ConversationService.update`.

Scénarios mock-first (pattern aligné `test_create_with_project_id.py` D3) :

1. Pydantic `ConversationUpdate` : project_id UUID + string accepté, null
   accepté, format invalide rejeté en 422, clear_project_id bool.
2. Service `update` attach : body project_id → ownership check passe,
   conv.project_id renseigné, commit appelé.
3. Service `update` detach : body clear_project_id=True → conv.project_id
   remis à None, ownership PROJET jamais appelé, commit appelé.
4. Service `update` 404 IDOR : `_get_owned_project` lève
   `ResourceNotFoundException`, propagation sans commit.
5. Service `update` clear prime sur attach : si les deux sont fournis,
   clear gagne (project_id ignoré, ownership jamais appelé).
6. Service `update` même projet → no-op (ownership validé mais aucun
   commit, project_changed=False + update_data vide).
7. Service `update` title + project_id : les deux appliqués, commit unique.
8. `ConversationResponse` / `ConversationListItem` exposent `project_id`.

Aucun Postgres réel requis (tests sub-seconde).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.auth.models import User
from app.features.chat.models import Conversation
from app.features.chat.schemas import (
    ConversationListItem,
    ConversationResponse,
    ConversationUpdate,
)
from app.features.chat.service import ConversationService

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _make_user(user_id: uuid.UUID | None = None) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id or uuid.uuid4()
    return user


def _make_conv(
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    title: str | None = "Brainstorm",
) -> Conversation:
    """ORM `Conversation` réel (pour que setattr + comparaisons project_id
    fonctionnent — un MagicMock(spec=...) renverrait un Mock truthy)."""
    now = datetime(2026, 6, 19, 10, 0, 0, tzinfo=UTC)
    conv = Conversation(user_id=user_id, title=title, expert_id="general")
    conv.id = uuid.uuid4()
    conv.project_id = project_id
    conv.last_message_at = None
    conv.message_count = 0
    conv.is_archived = False
    conv.is_favorite = False
    conv.title_generated_at = None
    conv.deleted_at = None
    conv.created_at = now
    conv.updated_at = now
    return conv


def _make_db() -> MagicMock:
    db = MagicMock(spec=AsyncSession)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock()
    return db


def _patch_owned_conv(monkeypatch, conv: Conversation) -> AsyncMock:
    mock = AsyncMock(return_value=conv)
    monkeypatch.setattr(ConversationService, "_get_owned_conversation", mock)
    return mock


def _patch_owned_project(monkeypatch, *, raises=None):
    from app.features.projects import service as projects_service

    if raises is not None:
        mock = AsyncMock(side_effect=raises)
    else:
        proj = MagicMock()
        mock = AsyncMock(return_value=proj)
    monkeypatch.setattr(projects_service.ProjectService, "_get_owned_project", mock)
    return mock


# ════════════════════════════════════════════════════════════════════
# Test 1 — Pydantic ConversationUpdate
# ════════════════════════════════════════════════════════════════════


class TestConversationUpdateSchema:
    def test_accepts_valid_uuid(self) -> None:
        pid = uuid.uuid4()
        body = ConversationUpdate(project_id=pid)
        assert body.project_id == pid
        assert body.clear_project_id is False

    def test_accepts_uuid_string_form(self) -> None:
        pid = uuid.uuid4()
        body = ConversationUpdate(project_id=str(pid))
        assert body.project_id == pid

    def test_accepts_null_default(self) -> None:
        body = ConversationUpdate()
        assert body.project_id is None
        assert body.clear_project_id is False

    def test_accepts_clear_flag(self) -> None:
        body = ConversationUpdate(clear_project_id=True)
        assert body.clear_project_id is True

    def test_rejects_invalid_format(self) -> None:
        with pytest.raises(ValidationError):
            ConversationUpdate(project_id="not-a-uuid")

    def test_exclude_unset_only_sent_fields(self) -> None:
        body = ConversationUpdate(project_id=uuid.uuid4())
        dumped = body.model_dump(exclude_unset=True)
        assert "project_id" in dumped
        assert "clear_project_id" not in dumped
        assert "title" not in dumped


# ════════════════════════════════════════════════════════════════════
# Test 2 — update attach (rattachement)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_attach_project(monkeypatch) -> None:
    user = _make_user()
    pid = uuid.uuid4()
    conv = _make_conv(user_id=user.id, project_id=None)
    _patch_owned_conv(monkeypatch, conv)
    get_owned_project = _patch_owned_project(monkeypatch)
    db = _make_db()

    body = ConversationUpdate(project_id=pid)
    result = await ConversationService.update(conv.id, body, user, db)

    get_owned_project.assert_awaited_once_with(pid, user.id, db)
    assert result.project_id == pid
    db.commit.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════
# Test 3 — update detach (clear_project_id)
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_detach_project(monkeypatch) -> None:
    user = _make_user()
    old_pid = uuid.uuid4()
    conv = _make_conv(user_id=user.id, project_id=old_pid)
    _patch_owned_conv(monkeypatch, conv)
    get_owned_project = _patch_owned_project(monkeypatch)
    db = _make_db()

    body = ConversationUpdate(clear_project_id=True)
    result = await ConversationService.update(conv.id, body, user, db)

    # Ownership PROJET jamais appelé sur un détachement.
    get_owned_project.assert_not_called()
    assert result.project_id is None
    db.commit.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════
# Test 4 — update attach 404 IDOR
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_attach_idor_safe(monkeypatch) -> None:
    user = _make_user()
    pid = uuid.uuid4()
    conv = _make_conv(user_id=user.id, project_id=None)
    _patch_owned_conv(monkeypatch, conv)
    get_owned_project = _patch_owned_project(
        monkeypatch, raises=ResourceNotFoundException("Projet")
    )
    db = _make_db()

    body = ConversationUpdate(project_id=pid)
    with pytest.raises(ResourceNotFoundException):
        await ConversationService.update(conv.id, body, user, db)

    get_owned_project.assert_awaited_once_with(pid, user.id, db)
    db.commit.assert_not_called()
    # La conv n'a pas été mutée (l'ownership check lève AVANT setattr).
    assert conv.project_id is None


# ════════════════════════════════════════════════════════════════════
# Test 5 — clear prime sur attach
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_clear_takes_precedence(monkeypatch) -> None:
    user = _make_user()
    old_pid = uuid.uuid4()
    new_pid = uuid.uuid4()
    conv = _make_conv(user_id=user.id, project_id=old_pid)
    _patch_owned_conv(monkeypatch, conv)
    get_owned_project = _patch_owned_project(monkeypatch)
    db = _make_db()

    body = ConversationUpdate(project_id=new_pid, clear_project_id=True)
    result = await ConversationService.update(conv.id, body, user, db)

    # clear gagne : project_id ignoré, ownership jamais appelé.
    get_owned_project.assert_not_called()
    assert result.project_id is None
    db.commit.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════
# Test 6 — même projet → no-op
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_same_project_noop(monkeypatch) -> None:
    user = _make_user()
    pid = uuid.uuid4()
    conv = _make_conv(user_id=user.id, project_id=pid)
    _patch_owned_conv(monkeypatch, conv)
    get_owned_project = _patch_owned_project(monkeypatch)
    db = _make_db()

    body = ConversationUpdate(project_id=pid)
    result = await ConversationService.update(conv.id, body, user, db)

    # Ownership re-validé, mais conv.project_id inchangé → aucun commit.
    get_owned_project.assert_awaited_once_with(pid, user.id, db)
    assert result.project_id == pid
    db.commit.assert_not_called()


# ════════════════════════════════════════════════════════════════════
# Test 7 — title + project_id combinés
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_title_and_project(monkeypatch) -> None:
    user = _make_user()
    pid = uuid.uuid4()
    conv = _make_conv(user_id=user.id, project_id=None, title="Avant")
    _patch_owned_conv(monkeypatch, conv)
    _patch_owned_project(monkeypatch)
    db = _make_db()

    body = ConversationUpdate(title="Après", project_id=pid)
    result = await ConversationService.update(conv.id, body, user, db)

    assert result.title == "Après"
    assert result.project_id == pid
    db.commit.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════
# Test 8 — schémas réponse exposent project_id
# ════════════════════════════════════════════════════════════════════


def test_response_schemas_expose_project_id() -> None:
    user_id = uuid.uuid4()
    pid = uuid.uuid4()
    conv = _make_conv(user_id=user_id, project_id=pid)

    resp = ConversationResponse.model_validate(conv)
    assert resp.project_id == pid

    item = ConversationListItem.model_validate(conv)
    assert item.project_id == pid

    # Conv sans projet → project_id None.
    conv2 = _make_conv(user_id=user_id, project_id=None)
    assert ConversationResponse.model_validate(conv2).project_id is None
    assert ConversationListItem.model_validate(conv2).project_id is None
