"""Tests unitaires — worker `dispatch_due_tasks` (F1)."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers import scheduler_tasks


class _FakeDB:
    def __init__(self, *, task_ids: list[uuid.UUID] | None = None) -> None:
        self._task_ids = task_ids or []
        self.executed_stmts: list[Any] = []
        self.commit = AsyncMock()

    async def __aenter__(self) -> _FakeDB:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def execute(self, stmt, *args, **kwargs):
        self.executed_stmts.append(stmt)
        sql = str(stmt).lower()
        result = MagicMock()
        if "select id" in sql and "for update" in sql:
            # Retourne une liste de tuples `(id,)`.
            rows = [(tid,) for tid in self._task_ids]
            result.all.return_value = rows
        return result


@pytest.fixture
def patch_db(monkeypatch: pytest.MonkeyPatch):
    def _patch(*, task_ids: list[uuid.UUID] | None = None) -> _FakeDB:
        db = _FakeDB(task_ids=task_ids or [])

        def _factory():
            return db

        monkeypatch.setattr(scheduler_tasks, "AsyncSessionLocal", _factory)
        return db

    return _patch


# ══════════════════════════════════════════════════════════════
# 1. Empty : aucune tâche due → dispatched=0
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_no_due_tasks_returns_zero(patch_db) -> None:
    patch_db(task_ids=[])
    result = await scheduler_tasks.dispatch_due_tasks({})
    assert result == {"dispatched": 0}


# ══════════════════════════════════════════════════════════════
# 2. Tasks dues → enqueue par task_id
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_enqueues_per_task(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    task_ids = [uuid.uuid4() for _ in range(3)]
    patch_db(task_ids=task_ids)

    enqueue_calls: list[uuid.UUID] = []

    async def _fake_enqueue(tid):
        enqueue_calls.append(tid)

    monkeypatch.setattr(scheduler_tasks, "enqueue_task_execution", _fake_enqueue)

    result = await scheduler_tasks.dispatch_due_tasks({})
    assert result == {"dispatched": 3}
    assert enqueue_calls == task_ids


# ══════════════════════════════════════════════════════════════
# 3. SQL contient SELECT FOR UPDATE SKIP LOCKED
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_sql_uses_for_update_skip_locked(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = patch_db(task_ids=[])
    monkeypatch.setattr(scheduler_tasks, "enqueue_task_execution", AsyncMock())
    await scheduler_tasks.dispatch_due_tasks({})
    # Au moins le SELECT doit avoir été exécuté.
    assert len(db.executed_stmts) >= 1
    first_sql = str(db.executed_stmts[0]).lower()
    assert "for update skip locked" in first_sql
    assert "next_run_at" in first_sql
    assert "deleted_at is null" in first_sql


# ══════════════════════════════════════════════════════════════
# 4. SQL filtre active + NOT paused + status NOT IN running/completed
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_sql_filters_active_paused_status(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = patch_db(task_ids=[])
    monkeypatch.setattr(scheduler_tasks, "enqueue_task_execution", AsyncMock())
    await scheduler_tasks.dispatch_due_tasks({})
    first_sql = str(db.executed_stmts[0]).lower()
    assert "active = true" in first_sql
    assert "paused = false" in first_sql
    assert "status not in" in first_sql


# ══════════════════════════════════════════════════════════════
# 5. Batch size respecté depuis settings
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_uses_configured_batch_size(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "tasks_dispatch_batch_size", 7, raising=False)
    db = patch_db(task_ids=[])
    monkeypatch.setattr(scheduler_tasks, "enqueue_task_execution", AsyncMock())
    await scheduler_tasks.dispatch_due_tasks({})
    # `.compile(literal_binds=True)` pour voir la valeur bindée.
    compiled = str(db.executed_stmts[0].compile(compile_kwargs={"literal_binds": True}))
    assert "LIMIT 7" in compiled or "limit 7" in compiled.lower()


# ══════════════════════════════════════════════════════════════
# 6. Enqueue fail-silent sur Redis down
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_enqueue_fail_silent_on_redis_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _broken_pool():
        raise RuntimeError("Redis refused")

    monkeypatch.setattr(scheduler_tasks, "_get_arq_pool", _broken_pool)
    # Ne doit pas raise.
    await scheduler_tasks.enqueue_task_execution(uuid.uuid4())


# ══════════════════════════════════════════════════════════════
# 7. Dispatch issue un UPDATE bulk status='pending'
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_issues_bulk_update_to_pending(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_ids = [uuid.uuid4() for _ in range(2)]
    db = patch_db(task_ids=task_ids)
    monkeypatch.setattr(scheduler_tasks, "enqueue_task_execution", AsyncMock())
    await scheduler_tasks.dispatch_due_tasks({})
    # Au moins 2 statements : SELECT puis UPDATE bulk.
    assert len(db.executed_stmts) >= 2
    update_sql = str(db.executed_stmts[1]).lower()
    assert "update scheduled_tasks" in update_sql
    db.commit.assert_awaited()


# ══════════════════════════════════════════════════════════════
# 8. Dispatch retourne stats dict
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_returns_stats_dict(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_db(task_ids=[uuid.uuid4()])
    monkeypatch.setattr(scheduler_tasks, "enqueue_task_execution", AsyncMock())
    result = await scheduler_tasks.dispatch_due_tasks({})
    assert isinstance(result, dict)
    assert "dispatched" in result
    assert result["dispatched"] == 1
