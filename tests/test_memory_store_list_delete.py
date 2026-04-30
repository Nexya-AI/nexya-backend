"""
Tests unitaires — extensions D5 de `MemoryStore` (`list_for_user` +
`delete_one_for_user`).

Inspection de la forme SQL via `literal_binds` pour `list_for_user` (pas
de DB réelle). Mocks séquentiels pour `delete_one_for_user`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import ValidationException
from app.features.memory.service import (
    MemoryStore,
    _decode_cursor,
    _encode_cursor,
)

_USER_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


def _make_user(is_pro: bool = False) -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = is_pro
    return user


class _FakeDB:
    """AsyncSession fake — séquentiel execute + add_all noop + commit."""

    def __init__(self, *, execute_results: list[Any]) -> None:
        self._results = iter(execute_results)
        self.executed_stmts: list[Any] = []
        self.commit = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):
        self.executed_stmts.append(stmt)
        return next(self._results)


class _ScalarsResult:
    """Mime de `result.scalars().all()` pour `list_for_user`."""

    def __init__(self, *, all_return: list[Any]) -> None:
        self._all = all_return

    def scalars(self):
        inner = MagicMock()
        inner.all.return_value = self._all
        return inner


# ══════════════════════════════════════════════════════════════
# 1. list_for_user — forme SQL avec filtre source + cursor
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_for_user_sql_shape_with_cursor_and_source_filter() -> None:
    """Vérifie que le SQL compilé inclut bien les filtres.

    Pas de DB : on capture le statement et on inspecte sa compilation.
    """
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarsResult(all_return=[])])

    cursor = _encode_cursor(
        datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC),
        uuid.uuid4(),
    )
    await MemoryStore.list_for_user(user, db, cursor=cursor, limit=10, source="manual")

    assert len(db.executed_stmts) == 1
    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "from memories" in compiled
    assert "deleted_at is null" in compiled
    assert "'manual'" in compiled  # source filter binded
    assert "order by" in compiled
    assert "desc" in compiled


# ══════════════════════════════════════════════════════════════
# 2. list_for_user — clause deleted_at IS NULL toujours présente
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_for_user_respects_deleted_at_is_null() -> None:
    user = _make_user()
    db = _FakeDB(execute_results=[_ScalarsResult(all_return=[])])
    await MemoryStore.list_for_user(user, db, cursor=None, limit=20)

    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "deleted_at is null" in compiled


# ══════════════════════════════════════════════════════════════
# 3. delete_one_for_user — émet un SQL DELETE (pas UPDATE)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_one_for_user_hard_deletes_via_sql_delete() -> None:
    """Vérifie qu'on émet bien un `DELETE FROM memories` (hard-delete).

    Un UPDATE `deleted_at = NOW()` serait une soft-delete (non-RGPD).
    """
    user = _make_user()
    # Le DELETE returning renvoie 1 id.
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = [uuid.uuid4()]
    result.scalars.return_value = scalars
    db = _FakeDB(execute_results=[result])

    count = await MemoryStore.delete_one_for_user(user, db, memory_id=uuid.uuid4())
    assert count == 1
    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    # Hard-delete strict : on vérifie la forme DELETE + absence d'UPDATE.
    assert compiled.startswith("delete from memories")
    assert "update memories" not in compiled
    db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 4. delete_one_for_user — idempotent (pas de 404 si absent)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_one_for_user_idempotent_when_already_missing() -> None:
    """Si aucune row ne match, retourne 0 sans raise."""
    user = _make_user()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = []  # Rien à supprimer.
    result.scalars.return_value = scalars
    db = _FakeDB(execute_results=[result])

    count = await MemoryStore.delete_one_for_user(user, db, memory_id=uuid.uuid4())
    assert count == 0  # idempotent, pas d'exception


# ══════════════════════════════════════════════════════════════
# 5. delete_one_for_user — scoped à l'owner
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_one_for_user_scoped_to_owner_only() -> None:
    """Le SQL filtre WHERE user_id — un attaquant ne peut pas DELETE
    une mémoire d'un autre user même s'il a son UUID."""
    user = _make_user()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = []
    result.scalars.return_value = scalars
    db = _FakeDB(execute_results=[result])

    await MemoryStore.delete_one_for_user(user, db, memory_id=uuid.uuid4())

    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True})).lower()
    assert "user_id" in compiled
    # SQLAlchemy rend les UUIDs sans dashes dans certains dialects —
    # on vérifie la forme sans dashes aussi.
    user_id_str = str(_USER_ID).replace("-", "")
    assert str(_USER_ID) in compiled or user_id_str in compiled


# ══════════════════════════════════════════════════════════════
# Bonus — helpers curseur
# ══════════════════════════════════════════════════════════════


def test_cursor_roundtrip_preserves_datetime_and_uuid() -> None:
    dt = datetime(2026, 4, 24, 12, 34, 56, tzinfo=UTC)
    rid = uuid.uuid4()
    cursor = _encode_cursor(dt, rid)
    recovered_dt, recovered_id = _decode_cursor(cursor)
    assert recovered_dt == dt
    assert recovered_id == rid


def test_cursor_malformed_raises_validation_exception() -> None:
    with pytest.raises(ValidationException):
        _decode_cursor("not-base64-$$$$")
    with pytest.raises(ValidationException):
        _decode_cursor("bm9waXBlCg==")  # pas de | dans le décodé
