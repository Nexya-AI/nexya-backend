"""
Tests unitaires B3 — `app.ai.engine.session_store` + `workers.ai_tasks`.

Couvre :
1. `SessionStore.record` : SET Redis avec TTL + payload JSON sérialisable.
2. `SessionStore.record` fail-safe : exception Redis swallowed.
3. `SessionStore.get` / `delete` : round-trip basique + fail-safe.
4. `SessionStore.scan_pending` : parse JSON, skip corrompu + delete corrompu,
   fail-safe si SCAN crash.
5. `flush_ai_sessions` (task arq) : scan → INSERT ON CONFLICT → delete Redis.
6. `_parse_uuid` / `_parse_decimal` : helpers tolérants.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai.engine.session_store import (
    SESSION_KEY_PREFIX,
    SESSION_TTL_SECONDS,
    SessionStore,
    get_session_store,
    reset_session_store_for_tests,
)
from workers import ai_tasks as ai_tasks_module
from workers.ai_tasks import (
    _parse_decimal,
    _parse_uuid,
    flush_ai_sessions,
)

# ══════════════════════════════════════════════════════════════
# Fake Redis client — capture les appels set/get/delete/scan_iter
# ══════════════════════════════════════════════════════════════


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.deleted: list[str] = []
        self.fail_on_set = False
        self.fail_on_get = False
        self.fail_on_delete = False
        self.fail_on_scan = False
        self._corrupt_keys: set[str] = set()

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail_on_set:
            raise ConnectionError("redis down")
        self.store[key] = value
        self.ttls[key] = ex or 0
        self.set_calls.append((key, value, ex))

    async def get(self, key: str) -> str | None:
        if self.fail_on_get:
            raise ConnectionError("redis down")
        if key in self._corrupt_keys:
            return "<<<not-json>>>"
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        if self.fail_on_delete:
            raise ConnectionError("redis down")
        self.deleted.append(key)
        self.store.pop(key, None)
        self._corrupt_keys.discard(key)
        return 1

    async def scan_iter(self, *, match: str, count: int = 200) -> AsyncIterator[str]:
        if self.fail_on_scan:
            raise ConnectionError("redis down")
        prefix = match.rstrip("*")
        for key in list(self.store.keys()) + list(self._corrupt_keys):
            if key.startswith(prefix):
                yield key

    def inject_corrupt(self, key: str) -> None:
        self._corrupt_keys.add(key)


# ══════════════════════════════════════════════════════════════
# 1. `SessionStore.record` — SET + TTL + payload JSON
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_writes_json_payload_with_ttl() -> None:
    fake = _FakeRedis()
    store = SessionStore(redis=fake)  # type: ignore[arg-type]

    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await store.record(
        session_id=session_id,
        user_id=user_id,
        trace_id="t-1",
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=0.0001,
        outcome="completed",
    )

    expected_key = f"{SESSION_KEY_PREFIX}{session_id}"
    assert expected_key in fake.store
    assert fake.ttls[expected_key] == SESSION_TTL_SECONDS
    payload = json.loads(fake.store[expected_key])
    assert payload["session_id"] == str(session_id)
    assert payload["user_id"] == str(user_id)
    assert payload["outcome"] == "completed"
    assert "stored_at" in payload


@pytest.mark.asyncio
async def test_record_serializes_user_id_none_as_null() -> None:
    fake = _FakeRedis()
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    sid = uuid.uuid4()
    await store.record(
        session_id=sid,
        user_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_usd=Decimal("0"),
        outcome="failed",
    )
    payload = json.loads(fake.store[f"{SESSION_KEY_PREFIX}{sid}"])
    assert payload["user_id"] is None


@pytest.mark.asyncio
async def test_record_is_failsafe_on_redis_error() -> None:
    fake = _FakeRedis()
    fake.fail_on_set = True
    store = SessionStore(redis=fake)  # type: ignore[arg-type]

    # Ne doit PAS raise — on log warning et on continue.
    await store.record(
        session_id=uuid.uuid4(),
        user_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
        outcome="completed",
    )


# ══════════════════════════════════════════════════════════════
# 2. `get` / `delete`
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_returns_parsed_payload_or_none() -> None:
    fake = _FakeRedis()
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    sid = uuid.uuid4()

    # Pas encore stocké → None.
    assert await store.get(sid) is None

    await store.record(
        session_id=sid,
        user_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.0,
        outcome="completed",
    )
    got = await store.get(sid)
    assert got is not None
    assert got["session_id"] == str(sid)


@pytest.mark.asyncio
async def test_get_failsafe_returns_none_on_redis_error() -> None:
    fake = _FakeRedis()
    fake.fail_on_get = True
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    assert await store.get(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_delete_removes_key() -> None:
    fake = _FakeRedis()
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    sid = uuid.uuid4()
    fake.store[f"{SESSION_KEY_PREFIX}{sid}"] = "{}"
    await store.delete(sid)
    assert f"{SESSION_KEY_PREFIX}{sid}" not in fake.store


@pytest.mark.asyncio
async def test_delete_is_failsafe() -> None:
    fake = _FakeRedis()
    fake.fail_on_delete = True
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    # Pas de raise.
    await store.delete(uuid.uuid4())


# ══════════════════════════════════════════════════════════════
# 3. `scan_pending` — SCAN + parse + skip corrompu
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_scan_pending_returns_valid_entries() -> None:
    fake = _FakeRedis()
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    for _ in range(3):
        await store.record(
            session_id=uuid.uuid4(),
            user_id=None,
            trace_id=None,
            expert_id="general",
            provider="openai",
            model="gpt-4o-mini",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost_usd=0.0,
            outcome="completed",
        )

    entries = await store.scan_pending()
    assert len(entries) == 3
    assert all("session_id" in e for e in entries)


@pytest.mark.asyncio
async def test_scan_pending_skips_and_deletes_corrupt_entries() -> None:
    fake = _FakeRedis()
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    fake.inject_corrupt(f"{SESSION_KEY_PREFIX}bad-1")

    await store.record(
        session_id=uuid.uuid4(),
        user_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.0,
        outcome="completed",
    )

    entries = await store.scan_pending()
    # Seule l'entrée saine est remontée.
    assert len(entries) == 1
    # La clé corrompue a été supprimée pour ne pas polluer les prochains flushs.
    assert f"{SESSION_KEY_PREFIX}bad-1" in fake.deleted


@pytest.mark.asyncio
async def test_scan_pending_failsafe_returns_empty_on_scan_error() -> None:
    fake = _FakeRedis()
    fake.fail_on_scan = True
    store = SessionStore(redis=fake)  # type: ignore[arg-type]
    assert await store.scan_pending() == []


# ══════════════════════════════════════════════════════════════
# 4. Singleton `get_session_store`
# ══════════════════════════════════════════════════════════════


def test_get_session_store_is_singleton() -> None:
    reset_session_store_for_tests()
    a = get_session_store()
    b = get_session_store()
    assert a is b
    reset_session_store_for_tests()


# ══════════════════════════════════════════════════════════════
# 5. Helpers `_parse_uuid` / `_parse_decimal`
# ══════════════════════════════════════════════════════════════


def test_parse_uuid_none_and_empty_return_none() -> None:
    assert _parse_uuid(None) is None
    assert _parse_uuid("") is None


def test_parse_uuid_valid_string_returns_uuid() -> None:
    sid = uuid.uuid4()
    assert _parse_uuid(str(sid)) == sid


def test_parse_uuid_invalid_returns_none() -> None:
    assert _parse_uuid("not-a-uuid") is None
    assert _parse_uuid(42) is None


def test_parse_decimal_handles_none_string_and_decimal() -> None:
    assert _parse_decimal(None) == Decimal("0")
    assert _parse_decimal("0.0001") == Decimal("0.0001")
    assert _parse_decimal(Decimal("0.0005")) == Decimal("0.0005")
    assert _parse_decimal("not-a-number") == Decimal("0")


# ══════════════════════════════════════════════════════════════
# 6. `flush_ai_sessions` — cron arq
# ══════════════════════════════════════════════════════════════


class _FakeFlushSession:
    """Session qui renvoie un `result` piloté pour le RETURNING id."""

    def __init__(self, *, returning_id: uuid.UUID | None) -> None:
        self._returning_id = returning_id
        self.executes: list[dict[str, Any]] = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self) -> _FakeFlushSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any]) -> Any:
        self.executes.append(params)
        result = MagicMock()
        # Premier execute = INSERT ai_calls avec RETURNING id.
        if len(self.executes) == 1:
            result.scalar_one_or_none.return_value = self._returning_id
        return result

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_flush_empty_store_returns_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_store = SessionStore(redis=fake_redis)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_tasks_module, "get_session_store", lambda: fake_store)

    stats = await flush_ai_sessions({})
    assert stats == {
        "scanned": 0,
        "inserted": 0,
        "skipped_duplicate": 0,
        "errors": 0,
    }


@pytest.mark.asyncio
async def test_flush_inserts_entry_and_deletes_key_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_store = SessionStore(redis=fake_redis)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_tasks_module, "get_session_store", lambda: fake_store)

    sid = uuid.uuid4()
    await fake_store.record(
        session_id=sid,
        user_id=uuid.uuid4(),
        trace_id="t-2",
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_usd=0.001,
        outcome="completed",
    )

    # INSERT renvoie un id → row insérée + UPSERT usage_daily sur outcome=completed.
    new_id = uuid.uuid4()
    fake_db_session = _FakeFlushSession(returning_id=new_id)
    monkeypatch.setattr(ai_tasks_module, "AsyncSessionLocal", lambda: fake_db_session)

    stats = await flush_ai_sessions({})

    assert stats["scanned"] == 1
    assert stats["inserted"] == 1
    assert stats["skipped_duplicate"] == 0
    assert stats["errors"] == 0
    # 2 execute : INSERT ai_calls + UPSERT usage_daily.
    assert len(fake_db_session.executes) == 2
    assert fake_db_session.commit_calls == 1
    # Clé Redis nettoyée.
    assert f"{SESSION_KEY_PREFIX}{sid}" in fake_redis.deleted


@pytest.mark.asyncio
async def test_flush_skips_upsert_usage_daily_when_fast_path_already_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_store = SessionStore(redis=fake_redis)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_tasks_module, "get_session_store", lambda: fake_store)

    sid = uuid.uuid4()
    await fake_store.record(
        session_id=sid,
        user_id=uuid.uuid4(),
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=0.0001,
        outcome="completed",
    )

    # RETURNING vide → fast path a déjà tenu → on saute le UPSERT.
    fake_db_session = _FakeFlushSession(returning_id=None)
    monkeypatch.setattr(ai_tasks_module, "AsyncSessionLocal", lambda: fake_db_session)

    stats = await flush_ai_sessions({})

    assert stats["inserted"] == 0
    assert stats["skipped_duplicate"] == 1
    # Un seul execute (INSERT ai_calls ON CONFLICT DO NOTHING). Pas de UPSERT.
    assert len(fake_db_session.executes) == 1
    assert fake_db_session.commit_calls == 1
    # Clé quand même nettoyée (la ligne DB existe côté fast path).
    assert f"{SESSION_KEY_PREFIX}{sid}" in fake_redis.deleted


@pytest.mark.asyncio
async def test_flush_skips_upsert_when_outcome_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_store = SessionStore(redis=fake_redis)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_tasks_module, "get_session_store", lambda: fake_store)

    sid = uuid.uuid4()
    await fake_store.record(
        session_id=sid,
        user_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
        outcome="failed",
    )

    fake_db_session = _FakeFlushSession(returning_id=uuid.uuid4())
    monkeypatch.setattr(ai_tasks_module, "AsyncSessionLocal", lambda: fake_db_session)

    stats = await flush_ai_sessions({})

    # Row insérée mais UPSERT usage_daily skippé (outcome=failed).
    assert stats["inserted"] == 1
    assert len(fake_db_session.executes) == 1


@pytest.mark.asyncio
async def test_flush_increments_errors_on_persist_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis()
    fake_store = SessionStore(redis=fake_redis)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_tasks_module, "get_session_store", lambda: fake_store)

    sid = uuid.uuid4()
    await fake_store.record(
        session_id=sid,
        user_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        cost_usd=0.0,
        outcome="completed",
    )

    async def boom(entry: dict[str, Any]) -> bool:
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(ai_tasks_module, "_persist_entry", boom)

    stats = await flush_ai_sessions({})

    assert stats["scanned"] == 1
    assert stats["errors"] == 1
    assert stats["inserted"] == 0
    # L'entrée est laissée en place pour re-try au prochain tick.
    assert f"{SESSION_KEY_PREFIX}{sid}" not in fake_redis.deleted


@pytest.mark.asyncio
async def test_flush_skips_entry_without_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scénario : entrée bizarre dans Redis sans session_id (ne devrait pas
    # arriver mais on veut tester la robustesse du parseur).
    fake_redis = _FakeRedis()
    fake_redis.store[f"{SESSION_KEY_PREFIX}weird"] = json.dumps(
        {"provider": "openai", "outcome": "completed"}
    )
    fake_store = SessionStore(redis=fake_redis)  # type: ignore[arg-type]
    monkeypatch.setattr(ai_tasks_module, "get_session_store", lambda: fake_store)

    stats = await flush_ai_sessions({})

    assert stats["scanned"] == 1
    assert stats["errors"] == 1
    assert stats["inserted"] == 0
