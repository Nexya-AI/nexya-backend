"""J1 — Tests du worker arq purge_deleted_accounts (~9 tests)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.rgpd_tasks as rgpd_tasks
from workers.rgpd_tasks import (
    _collect_storage_keys,
    _delete_blobs,
    _process_one,
    purge_deleted_accounts,
)


class _ScalarResult:
    def __init__(self, *, all_rows=None, rowcount=0):
        self._all = all_rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._all


def _mk_db_for_purge(
    *,
    requests=None,
    storage_keys_uploaded=None,
    storage_keys_library=None,
    rowcount=1,
):
    db = MagicMock()
    # execute() séquence :
    # 1. SELECT pending pour batch (with_for_update)
    # 2. (per request) mark_processing flush — pas d'execute, juste flush
    # 3. (per request) SELECT uploaded_files.storage_key
    # 4. (per request) SELECT library_items.storage_key
    # 5. (per request) DELETE FROM users
    # 6. (per request) UPDATE deletion_requests (mark_completed flush)
    side_effects = []
    side_effects.append(_ScalarResult(all_rows=requests or []))
    for _ in requests or []:
        side_effects.append(_ScalarResult(all_rows=storage_keys_uploaded or []))
        side_effects.append(_ScalarResult(all_rows=storage_keys_library or []))
        # DELETE
        result = MagicMock()
        result.rowcount = rowcount
        side_effects.append(result)
    db.execute = AsyncMock(side_effect=side_effects)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    db.get = AsyncMock()
    return db


def _mk_request(scheduled_minus_minutes=10):
    req = MagicMock()
    req.id = uuid.uuid4()
    req.user_id = uuid.uuid4()
    req.status = "pending"
    req.scheduled_purge_at = datetime.now(UTC) - timedelta(minutes=scheduled_minus_minutes)
    req.purge_summary_json = {"email_for_confirmation": "test@x.y"}
    req.updated_at = None
    return req


@pytest.mark.asyncio
async def test_collect_storage_keys_uploads_and_library():
    user_id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(all_rows=["uploads/a.pdf", "uploads/b.docx"]),
            _ScalarResult(all_rows=["library/img.png"]),
        ]
    )
    keys = await _collect_storage_keys(user_id, db)
    assert keys == ["uploads/a.pdf", "uploads/b.docx", "library/img.png"]


@pytest.mark.asyncio
async def test_delete_blobs_failsafe_continues_on_exception(monkeypatch):
    deleted_calls = []

    async def fake_delete(key):
        deleted_calls.append(key)
        if key == "broken":
            raise RuntimeError("MinIO 503")

    store = MagicMock()
    store.delete_object = AsyncMock(side_effect=fake_delete)
    monkeypatch.setattr(rgpd_tasks, "get_object_store", lambda: store)

    deleted = await _delete_blobs(["a.png", "broken", "b.pdf"])
    assert deleted == 2  # a.png + b.pdf, broken a échoué silencieux
    assert deleted_calls == ["a.png", "broken", "b.pdf"]


@pytest.mark.asyncio
async def test_process_one_returns_summary(monkeypatch):
    request = _mk_request()
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(all_rows=["k1"]),  # uploaded_files
            _ScalarResult(all_rows=["k2"]),  # library_items
            _ScalarResult(rowcount=1),  # DELETE users
        ]
    )
    db.execute.side_effect = [
        _ScalarResult(all_rows=["k1"]),
        _ScalarResult(all_rows=["k2"]),
        MagicMock(rowcount=1),
    ]

    store = MagicMock()
    store.delete_object = AsyncMock()
    monkeypatch.setattr(rgpd_tasks, "get_object_store", lambda: store)

    summary = await _process_one(request, db)
    assert summary["users_deleted"] == 1
    assert summary["tables_purged"] == 22
    assert summary["blobs_total"] == 2
    assert summary["blobs_deleted"] == 2
    assert "duration_ms" in summary


@pytest.mark.asyncio
async def test_process_one_no_user_no_tables_purged(monkeypatch):
    request = _mk_request()
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(all_rows=[]),
            _ScalarResult(all_rows=[]),
            MagicMock(rowcount=0),  # user déjà supprimé
        ]
    )

    store = MagicMock()
    store.delete_object = AsyncMock()
    monkeypatch.setattr(rgpd_tasks, "get_object_store", lambda: store)

    summary = await _process_one(request, db)
    assert summary["users_deleted"] == 0
    assert summary["tables_purged"] == 0


@pytest.mark.asyncio
async def test_purge_cron_empty_queue_returns_zero(monkeypatch):
    db = _mk_db_for_purge(requests=[])
    monkeypatch.setattr(rgpd_tasks, "AsyncSessionLocal", lambda: _async_ctx(db))
    result = await purge_deleted_accounts({})
    assert result == {"processed": 0, "completed": 0, "failed": 0}


@pytest.mark.asyncio
async def test_purge_cron_one_request_completed(monkeypatch):
    request = _mk_request()
    db = _mk_db_for_purge(
        requests=[request],
        storage_keys_uploaded=["uploads/a"],
        storage_keys_library=["lib/b"],
        rowcount=1,
    )
    monkeypatch.setattr(rgpd_tasks, "AsyncSessionLocal", lambda: _async_ctx(db))
    store = MagicMock()
    store.delete_object = AsyncMock()
    monkeypatch.setattr(rgpd_tasks, "get_object_store", lambda: store)

    result = await purge_deleted_accounts({})
    assert result["completed"] == 1
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_purge_cron_request_with_exception_marked_failed(monkeypatch):
    """Si _process_one raise, la request est marquée failed dans une
    nouvelle session, et le cron continue."""
    request = _mk_request()

    # DB principal qui lève sur le DELETE
    main_db = MagicMock()
    main_db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(all_rows=[request]),  # SELECT pending
            _ScalarResult(all_rows=[]),  # uploaded_files
            _ScalarResult(all_rows=[]),  # library_items
            RuntimeError("DB blew up"),  # DELETE FROM users
        ]
    )
    main_db.commit = AsyncMock()
    main_db.rollback = AsyncMock()
    main_db.flush = AsyncMock()
    main_db.refresh = AsyncMock()
    main_db.add = MagicMock()
    main_db.get = AsyncMock()

    # DB pour mark_failed (nouvelle session)
    fail_db = MagicMock()
    fresh_request = MagicMock()
    fresh_request.purge_summary_json = None
    fail_db.get = AsyncMock(return_value=fresh_request)
    fail_db.flush = AsyncMock()
    fail_db.commit = AsyncMock()

    sessions = iter([main_db, fail_db])
    monkeypatch.setattr(rgpd_tasks, "AsyncSessionLocal", lambda: _async_ctx(next(sessions)))

    result = await purge_deleted_accounts({})
    assert result["failed"] == 1
    assert result["completed"] == 0
    main_db.rollback.assert_awaited()
    assert fresh_request.status == "failed"


@pytest.mark.asyncio
async def test_purge_cron_uses_for_update_skip_locked(monkeypatch):
    """Vérifie que la requête SELECT utilise with_for_update(skip_locked=True)."""
    db = _mk_db_for_purge(requests=[])
    monkeypatch.setattr(rgpd_tasks, "AsyncSessionLocal", lambda: _async_ctx(db))
    await purge_deleted_accounts({})
    # `SKIP LOCKED` est rendu par le dialecte Postgres (pas le générique).
    from sqlalchemy.dialects import postgresql

    call = db.execute.call_args_list[0]
    stmt = call.args[0]
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FOR UPDATE" in compiled.upper()
    assert "SKIP LOCKED" in compiled.upper()


@pytest.mark.asyncio
async def test_purge_cron_filters_by_scheduled_purge_at_passed(monkeypatch):
    """Le SELECT doit filtrer status='pending' AND scheduled_purge_at <= NOW()."""
    db = _mk_db_for_purge(requests=[])
    monkeypatch.setattr(rgpd_tasks, "AsyncSessionLocal", lambda: _async_ctx(db))
    await purge_deleted_accounts({})
    call = db.execute.call_args_list[0]
    stmt = call.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "status" in compiled.lower()
    assert "scheduled_purge_at" in compiled.lower()


def _async_ctx(db):
    """Wrapper async context manager pour AsyncSessionLocal."""

    class _Ctx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return None

    return _Ctx()
