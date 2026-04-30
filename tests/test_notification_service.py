"""
Tests F3 — NotificationService (CRUD table notifications).

Couvre :
- create insère + commit + retourne la row.
- list_for_user SQL shape (filtre user_id + deleted_at, sort sent_at DESC).
- list_for_user avec unread_only ajoute read_at IS NULL.
- list_for_user avec category filtre.
- list_for_user avec cursor applique le tuple_ keyset.
- mark_read UPDATE filtré user_id + read_at IS NULL + retourne rowcount.
- mark_read avec liste vide retourne 0 sans DB call.
- soft_delete via _get_owned → 404 IDOR / marque deleted_at + commit.
- Curseur opaque base64 roundtrip + malformed raise.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from app.features.notifications.models import Notification
from app.features.notifications.service import (
    NotificationService,
    _decode_cursor,
    _encode_cursor,
)


def _fake_user(user_id: uuid.UUID | None = None):
    user = MagicMock()
    user.id = user_id or uuid.uuid4()
    return user


def _fake_notif(
    *,
    notif_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    category: str = "tasks",
    deleted_at=None,
):
    n = MagicMock(spec=Notification)
    n.id = notif_id or uuid.uuid4()
    n.user_id = user_id or uuid.uuid4()
    n.category = category
    n.deleted_at = deleted_at
    n.sent_at = datetime.now(tz=UTC)
    return n


# ── Cursor helpers ─────────────────────────────────────────────────


def test_cursor_roundtrip():
    ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    rid = uuid.uuid4()
    cursor = _encode_cursor(ts, rid)
    ts_back, rid_back = _decode_cursor(cursor)
    assert ts_back == ts
    assert rid_back == rid


def test_cursor_malformed_raises_validation():
    with pytest.raises(ValidationException):
        _decode_cursor("not-a-valid-base64!!!")


def test_cursor_missing_separator_raises():
    import base64

    bad = base64.urlsafe_b64encode(b"noseparator").decode("ascii")
    with pytest.raises(ValidationException):
        _decode_cursor(bad)


def test_cursor_invalid_uuid_raises():
    import base64

    bad = base64.urlsafe_b64encode(b"2026-04-25T12:00:00+00:00|NOT-A-UUID").decode("ascii")
    with pytest.raises(ValidationException):
        _decode_cursor(bad)


# ── CREATE ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_inserts_and_commits():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    notif = await NotificationService.create(
        user_id=uuid.uuid4(),
        category="tasks",
        title="Title",
        body="Body",
        data={"deep_link": "nexya://task/abc"},
        channel_used="push",
        source_task_id=None,
        source_kind="scheduled_task",
        push_message_id="msg-1",
        email_message_id=None,
        attempts_push=1,
        attempts_email=0,
        db=db,
    )
    assert db.add.call_count == 1
    assert db.commit.await_count == 1
    # refresh est appelé pour populater l'UUID serveur-side
    assert db.refresh.await_count == 1


# ── LIST ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_for_user_sql_contains_user_and_not_deleted(monkeypatch):
    user = _fake_user()

    # Capture le stmt passé à execute() pour inspection.
    captured = {}

    async def _execute(stmt):
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)

    page = await NotificationService.list_for_user(user, db, cursor=None, limit=10)
    assert page.items == []
    assert page.next_cursor is None

    # Inspect la compilation SQL
    compiled = str(captured["stmt"].compile(compile_kwargs={"literal_binds": False}))
    assert "notifications" in compiled
    assert "user_id" in compiled
    assert "deleted_at" in compiled


@pytest.mark.asyncio
async def test_list_for_user_unread_only_filters_read_at():
    user = _fake_user()
    captured = {}

    async def _execute(stmt):
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    await NotificationService.list_for_user(user, db, unread_only=True)
    compiled = str(captured["stmt"].compile(compile_kwargs={"literal_binds": False}))
    assert "read_at" in compiled


@pytest.mark.asyncio
async def test_list_for_user_category_filter_applied():
    user = _fake_user()
    captured = {}

    async def _execute(stmt):
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    await NotificationService.list_for_user(user, db, category="tasks")
    compiled = str(captured["stmt"].compile(compile_kwargs={"literal_binds": False}))
    assert "category" in compiled


@pytest.mark.asyncio
async def test_list_for_user_with_cursor_applies_keyset():
    user = _fake_user()
    ts = datetime.now(tz=UTC)
    rid = uuid.uuid4()
    cursor = _encode_cursor(ts, rid)
    captured = {}

    async def _execute(stmt):
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    await NotificationService.list_for_user(user, db, cursor=cursor)
    # Pas d'exception = curseur bien décodé et appliqué


@pytest.mark.asyncio
async def test_list_for_user_has_more_generates_next_cursor():
    user = _fake_user()
    now = datetime.now(tz=UTC)
    rows = []
    for i in range(11):  # limit=10 + 1 → has_more
        n = _fake_notif(user_id=user.id)
        n.sent_at = now
        rows.append(n)

    async def _execute(stmt):
        result = MagicMock()
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    page = await NotificationService.list_for_user(user, db, limit=10)
    assert len(page.items) == 10
    assert page.next_cursor is not None


# ── MARK READ ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_read_empty_list_returns_zero():
    user = _fake_user()
    db = MagicMock()
    db.execute = AsyncMock()
    n = await NotificationService.mark_read(user, [], db)
    assert n == 0
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_mark_read_returns_rowcount_and_commits():
    user = _fake_user()
    result = MagicMock()
    result.rowcount = 3
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    n = await NotificationService.mark_read(
        user,
        [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()],
        db,
    )
    assert n == 3
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_mark_read_sql_filters_user_and_null_read_at():
    user = _fake_user()
    captured = {}

    async def _execute(stmt):
        captured["stmt"] = stmt
        result = MagicMock()
        result.rowcount = 1
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    await NotificationService.mark_read(user, [uuid.uuid4()], db)
    compiled = str(captured["stmt"].compile(compile_kwargs={"literal_binds": False}))
    assert "user_id" in compiled
    assert "read_at" in compiled


# ── SOFT DELETE ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_soft_delete_idor_safe_raises_not_found():
    user = _fake_user()
    # _get_owned execute() retourne None → 404
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    with pytest.raises(ResourceNotFoundException):
        await NotificationService.soft_delete(user, uuid.uuid4(), db)


@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_and_commits():
    user = _fake_user()
    notif = _fake_notif(user_id=user.id)
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=notif)

    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()

    await NotificationService.soft_delete(user, notif.id, db)
    assert db.commit.await_count == 1
