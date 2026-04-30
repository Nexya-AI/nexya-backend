"""Tests unitaires — cron `cleanup_old_task_results` (F1)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers import scheduler_tasks


class _FakeDB:
    def __init__(self, *, deleted_ids: list[int] | None = None) -> None:
        self._deleted = deleted_ids or []
        self.executed_stmts: list[Any] = []
        self.commit = AsyncMock()

    async def __aenter__(self) -> _FakeDB:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def execute(self, stmt, *args, **kwargs):
        self.executed_stmts.append(stmt)
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = self._deleted
        result.scalars.return_value = scalars
        return result


@pytest.fixture
def patch_db(monkeypatch: pytest.MonkeyPatch):
    def _patch(*, deleted_ids: list[int] | None = None) -> _FakeDB:
        db = _FakeDB(deleted_ids=deleted_ids or [])

        def _factory():
            return db

        monkeypatch.setattr(scheduler_tasks, "AsyncSessionLocal", _factory)
        return db

    return _patch


@pytest.mark.asyncio
async def test_cleanup_no_rows_returns_zero(patch_db) -> None:
    patch_db(deleted_ids=[])
    result = await scheduler_tasks.cleanup_old_task_results({})
    assert result == {"deleted": 0}


@pytest.mark.asyncio
async def test_cleanup_deletes_old_rows(patch_db) -> None:
    patch_db(deleted_ids=[1, 2, 3])
    result = await scheduler_tasks.cleanup_old_task_results({})
    assert result == {"deleted": 3}


@pytest.mark.asyncio
async def test_cleanup_sql_is_delete_on_results_table(patch_db) -> None:
    db = patch_db(deleted_ids=[])
    await scheduler_tasks.cleanup_old_task_results({})
    assert len(db.executed_stmts) == 1
    sql = str(db.executed_stmts[0]).lower()
    assert "delete from scheduled_task_results" in sql
    assert "ran_at <" in sql


@pytest.mark.asyncio
async def test_cleanup_uses_configured_retention(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "tasks_results_retention_days", 60, raising=False)
    db = patch_db(deleted_ids=[])
    await scheduler_tasks.cleanup_old_task_results({})
    db.commit.assert_awaited_once()
