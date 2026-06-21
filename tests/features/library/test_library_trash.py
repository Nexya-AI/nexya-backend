"""Tests C3.5 — Corbeille Library (`list_trash_for_user` / `restore` /
`permanent_delete` + `_get_owned_item_in_trash`).

Miroir strict de la corbeille Conversations (B3). Couvre :

1. `_get_owned_item_in_trash` SQL → filtre `deleted_at IS NOT NULL`.
2. `_get_owned_item_in_trash` 404 si introuvable.
3. `list_trash_for_user` SQL → `deleted_at IS NOT NULL` + `ORDER BY ... DESC`.
4. `list_trash_for_user` filtre type_ injecté.
5. `list_trash_for_user` next_cursor sur overflow.
6. `restore` happy → deleted_at=None + commit.
7. `restore` 404 si pas dans la corbeille.
8. `permanent_delete` happy → db.delete + commit.
9. `permanent_delete` 404 si pas dans la corbeille.

Aucun Postgres réel requis (SQL compilé via literal_binds + fake DB).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user() -> MagicMock:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = False
    return user


def _make_trashed_item(*, item_id: uuid.UUID | None = None) -> LibraryItem:
    now = datetime(2026, 6, 19, 10, 0, 0, tzinfo=UTC)
    item = LibraryItem(
        user_id=_USER_ID,
        type="image",
        title="Image supprimée",
        storage_key="c4a2.../library/image/ab/abcd.png",
        mime_type="image/png",
        size_bytes=100,
        content_sha256="a" * 64,
        source="generated",
    )
    item.id = item_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
    item.created_at = now
    item.updated_at = now
    item.deleted_at = now  # soft-deleté (en corbeille)
    item.file_type = None
    item.description = None
    item.width_px = None
    item.height_px = None
    item.duration_ms = None
    item.aspect_ratio = None
    item.provider = "gemini-imagen"
    item.model = "imagen-3.0"
    item.prompt = "x"
    item.source_conversation_id = None
    item.source_message_id = None
    item.tags = None
    item.metadata_json = None
    item.parent_library_id = None
    return item


def _capture_db(rows: list[LibraryItem] | None = None) -> tuple[MagicMock, dict]:
    captured: dict = {}

    async def _execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        r = MagicMock()
        r.scalars.return_value.all.return_value = rows or []
        return r

    db = MagicMock()
    db.execute = _execute
    return db, captured


def _scalar_db(item: LibraryItem | None) -> MagicMock:
    """Fake DB dont le 1ᵉʳ execute renvoie scalar_one_or_none=item."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = item
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


def _compiled_sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


# ════════════════════════════════════════════════════════════════════
# 1-2. _get_owned_item_in_trash
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_owned_item_in_trash_sql_filters_deleted_not_null() -> None:
    item = _make_trashed_item()
    db, captured = _capture_db()
    # scalars().all() vide → on doit court-circuiter le scalar_one_or_none.
    # On utilise plutôt un scalar_db pour la valeur, mais on veut le SQL :
    db2 = _scalar_db(item)

    # Capture le stmt via un wrapper.
    captured2: dict = {}
    orig = db2.execute

    async def _wrap(stmt, *a, **k):
        captured2["stmt"] = stmt
        return await orig(stmt, *a, **k)

    db2.execute = _wrap
    await LibraryService._get_owned_item_in_trash(item.id, _USER_ID, db2)
    sql = _compiled_sql(captured2["stmt"])
    assert "deleted_at is not null" in sql
    assert "user_id" in sql


@pytest.mark.asyncio
async def test_get_owned_item_in_trash_404_when_missing() -> None:
    db = _scalar_db(None)
    with pytest.raises(ResourceNotFoundException):
        await LibraryService._get_owned_item_in_trash(uuid.uuid4(), _USER_ID, db)


# ════════════════════════════════════════════════════════════════════
# 3-5. list_trash_for_user
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_trash_sql_filters_and_orders() -> None:
    db, captured = _capture_db([])
    await LibraryService.list_trash_for_user(_make_user(), db)
    sql = _compiled_sql(captured["stmt"])
    assert "deleted_at is not null" in sql
    # Tri par deleted_at DESC.
    assert "order by" in sql
    assert "deleted_at desc" in sql


@pytest.mark.asyncio
async def test_list_trash_type_filter_injected() -> None:
    db, captured = _capture_db([])
    await LibraryService.list_trash_for_user(_make_user(), db, type_="video")
    sql = _compiled_sql(captured["stmt"])
    assert "library_items.type" in sql
    assert "'video'" in sql


@pytest.mark.asyncio
async def test_list_trash_next_cursor_on_overflow() -> None:
    # limit=2 → on demande 3, on retourne 3 rows → has_next=True.
    rows = [
        _make_trashed_item(item_id=uuid.UUID(f"aaaaaaaa-0000-4000-8000-00000000000{i}"))
        for i in range(1, 4)
    ]
    db, _ = _capture_db(rows)
    page = await LibraryService.list_trash_for_user(_make_user(), db, limit=2)
    assert len(page.items) == 2
    assert page.next_cursor is not None


@pytest.mark.asyncio
async def test_list_trash_no_cursor_when_no_overflow() -> None:
    rows = [_make_trashed_item()]
    db, _ = _capture_db(rows)
    page = await LibraryService.list_trash_for_user(_make_user(), db, limit=20)
    assert len(page.items) == 1
    assert page.next_cursor is None


# ════════════════════════════════════════════════════════════════════
# 6-7. restore
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_restore_clears_deleted_at() -> None:
    user = _make_user()
    item = _make_trashed_item()
    db = _scalar_db(item)

    result = await LibraryService.restore(item.id, user, db)
    assert result.deleted_at is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_404_when_not_in_trash() -> None:
    user = _make_user()
    db = _scalar_db(None)
    with pytest.raises(ResourceNotFoundException):
        await LibraryService.restore(uuid.uuid4(), user, db)
    db.commit.assert_not_called()


# ════════════════════════════════════════════════════════════════════
# 8-9. permanent_delete
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_permanent_delete_deletes_row() -> None:
    user = _make_user()
    item = _make_trashed_item()
    db = _scalar_db(item)

    await LibraryService.permanent_delete(item.id, user, db)
    db.delete.assert_awaited_once_with(item)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanent_delete_404_when_not_in_trash() -> None:
    user = _make_user()
    db = _scalar_db(None)
    with pytest.raises(ResourceNotFoundException):
        await LibraryService.permanent_delete(uuid.uuid4(), user, db)
    db.delete.assert_not_called()
    db.commit.assert_not_called()
