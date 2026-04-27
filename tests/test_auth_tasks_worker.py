"""
Tests N2 — `workers.auth_tasks.cleanup_refresh_tokens` cron.

Ce worker tourne via arq cron quotidien (3h17 UTC) pour purger la table
`refresh_tokens` de ses entrées périmées :
- expirés depuis > 1 jour (`EXPIRED_RETENTION`)
- révoqués depuis > 7 jours (`REVOKED_RETENTION`)

On teste :
1. Le SQL généré contient bien la clause OR sur `expires_at < cutoff`
   et `revoked_at IS NOT NULL AND revoked_at < cutoff`.
2. Retourne `{"deleted": N}` avec N issu de `result.rowcount`.
3. Fail-safe sur `rowcount=None` (driver qui ne supporte pas) → 0.
4. Commit appelé une fois après le DELETE.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers import auth_tasks


class _FakeAsyncSession:
    def __init__(self, rowcount: int | None = 0) -> None:
        self.executes: list[Any] = []
        self.commit_calls = 0
        self._result = MagicMock(rowcount=rowcount)

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        self.executes.append(stmt)
        return self._result

    async def commit(self) -> None:
        self.commit_calls += 1


def _patch_session(monkeypatch: pytest.MonkeyPatch, session: _FakeAsyncSession) -> None:
    monkeypatch.setattr(auth_tasks, "AsyncSessionLocal", lambda: session)


@pytest.mark.asyncio
async def test_cleanup_refresh_tokens_returns_dict_with_deleted_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession(rowcount=42)
    _patch_session(monkeypatch, session)

    result = await auth_tasks.cleanup_refresh_tokens(ctx={})

    assert result == {"deleted": 42}
    assert session.commit_calls == 1
    assert len(session.executes) == 1


@pytest.mark.asyncio
async def test_cleanup_refresh_tokens_handles_none_rowcount_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Certains drivers SQLAlchemy retournent rowcount=None — on doit
    coercer à 0 (pas de crash, pas de None dans le dict de retour)."""
    session = _FakeAsyncSession(rowcount=None)
    _patch_session(monkeypatch, session)

    result = await auth_tasks.cleanup_refresh_tokens(ctx={})

    assert result == {"deleted": 0}
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_cleanup_refresh_tokens_compiled_sql_targets_refresh_tokens_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le SQL compilé doit cibler la table `refresh_tokens` et combiner
    les 2 critères (expires_at OR revoked_at) via OR."""
    session = _FakeAsyncSession(rowcount=0)
    _patch_session(monkeypatch, session)

    await auth_tasks.cleanup_refresh_tokens(ctx={})

    stmt = session.executes[0]
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "delete from refresh_tokens" in sql
    assert "expires_at" in sql
    assert "revoked_at" in sql
    assert " or " in sql


@pytest.mark.asyncio
async def test_cleanup_refresh_tokens_uses_expected_retention_constants() -> None:
    """Les constantes EXPIRED_RETENTION (1j) et REVOKED_RETENTION (7j) sont
    le contrat documenté côté ops — anti-régression."""
    from datetime import timedelta

    assert auth_tasks.EXPIRED_RETENTION == timedelta(days=1)
    assert auth_tasks.REVOKED_RETENTION == timedelta(days=7)
