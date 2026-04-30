"""
Tests unitaires B3 — `app.ai.cost_tracker`.

Couvre :
1. `_to_decimal` / `_jsonify` — conversions safe.
2. `CostTracker.record_ai_call` — contrat fail-safe (jamais raise).
3. `record_ai_call_background` — schedule bien une asyncio.Task.
4. `_persist` — pipeline INSERT ai_calls + UPSERT usage_daily :
   - UPSERT usage_daily UNIQUEMENT pour outcome ∈ {completed, cancelled}.
   - IntegrityError UNIQUE session_id → rollback + log + return sans raise.
   - SQLAlchemyError autre → swallowed par `record_ai_call` (fail-safe).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.ai import cost_tracker as cost_tracker_module
from app.ai.cost_tracker import CostTracker, _jsonify, _to_decimal

# ══════════════════════════════════════════════════════════════
# Fake AsyncSession — reproduit le protocole `async with AsyncSessionLocal()`
# ══════════════════════════════════════════════════════════════


class _FakeAsyncSession:
    """Session asyncio fake — enregistre les execute/commit/rollback pour assertions."""

    def __init__(self, *, raise_on_first_execute: Exception | None = None) -> None:
        self.executes: list[tuple[Any, dict[str, Any]]] = []
        self.commit_calls = 0
        self.rollback_calls = 0
        self._raise_on_first_execute = raise_on_first_execute

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.executes.append((stmt, params or {}))
        if self._raise_on_first_execute and len(self.executes) == 1:
            exc = self._raise_on_first_execute
            self._raise_on_first_execute = None
            raise exc
        return MagicMock()

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _install_fake_session(monkeypatch: pytest.MonkeyPatch, session: _FakeAsyncSession) -> None:
    """Remplace `AsyncSessionLocal` dans le module cost_tracker par un factory qui
    renvoie toujours la même session fake (le context-manager `async with` est
    géré par `_FakeAsyncSession` lui-même)."""

    monkeypatch.setattr(cost_tracker_module, "AsyncSessionLocal", lambda: session)


# ══════════════════════════════════════════════════════════════
# 1. Helpers `_to_decimal` et `_jsonify`
# ══════════════════════════════════════════════════════════════


def test_to_decimal_float_goes_via_str_to_avoid_ieee754_drift() -> None:
    # La conversion `Decimal(0.1)` donne Decimal('0.10000…555'). On passe par
    # str() pour préserver la représentation humaine.
    assert _to_decimal(0.1) == Decimal("0.1")


def test_to_decimal_keeps_decimal_as_is() -> None:
    d = Decimal("0.000123")
    assert _to_decimal(d) is d


def test_to_decimal_zero_falsy_becomes_decimal_zero() -> None:
    assert _to_decimal(0) == Decimal("0")
    assert _to_decimal(0.0) == Decimal("0")


def test_jsonify_none_returns_none() -> None:
    assert _jsonify(None) is None


def test_jsonify_dict_returns_json_string() -> None:
    result = _jsonify({"a": 1, "b": "deux"})
    assert isinstance(result, str)
    assert json.loads(result) == {"a": 1, "b": "deux"}


def test_jsonify_handles_non_serializable_via_default_str() -> None:
    uid = uuid.uuid4()
    result = _jsonify({"sid": uid})
    assert json.loads(result) == {"sid": str(uid)}


# ══════════════════════════════════════════════════════════════
# 2. `record_ai_call_background` — schedule une Task
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_background_schedules_a_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = CostTracker()
    recorded: dict[str, Any] = {}

    async def fake_record(**kwargs: Any) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(tracker, "record_ai_call", fake_record)

    task = tracker.record_ai_call_background(
        user_id=None,
        session_id=None,
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

    assert isinstance(task, asyncio.Task)
    await task
    assert recorded["expert_id"] == "general"
    assert recorded["outcome"] == "completed"


# ══════════════════════════════════════════════════════════════
# 3. `record_ai_call` — fail-safe, pipeline, UPSERT conditionnel
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_completed_inserts_and_upserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    await tracker.record_ai_call(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        trace_id="t-1",
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost_usd=0.0023,
        outcome="completed",
    )

    # 2 execute : INSERT ai_calls + UPSERT usage_daily.
    assert len(session.executes) == 2
    assert session.commit_calls == 1
    assert session.rollback_calls == 0


@pytest.mark.asyncio
async def test_record_ai_call_failed_skips_usage_daily_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    await tracker.record_ai_call(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
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

    # Un seul execute : INSERT ai_calls. Pas d'UPSERT usage_daily sur failed.
    assert len(session.executes) == 1
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_record_ai_call_cancelled_does_upsert_usage_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    await tracker.record_ai_call(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        trace_id=None,
        expert_id="general",
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_tokens=80,
        completion_tokens=20,
        total_tokens=100,
        cost_usd=Decimal("0.0001"),
        outcome="cancelled",
    )

    assert len(session.executes) == 2  # INSERT ai_calls + UPSERT usage_daily
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_record_ai_call_integrity_error_on_duplicate_session_id_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_integrity = IntegrityError("UNIQUE", {}, Exception("dup"))
    session = _FakeAsyncSession(raise_on_first_execute=fake_integrity)
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    # Ne doit PAS raise — le doublon est logué et ignoré.
    await tracker.record_ai_call(
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
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

    # rollback appelé, pas de commit, pas d'UPSERT usage_daily.
    assert session.rollback_calls == 1
    assert session.commit_calls == 0
    assert len(session.executes) == 1


@pytest.mark.asyncio
async def test_record_ai_call_sqlalchemy_error_is_swallowed_by_failsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Une erreur SQL non-IntegrityError remonte dans `_persist` → attrapée
    # par le try/except global de `record_ai_call` (fail-safe).
    session = _FakeAsyncSession(raise_on_first_execute=SQLAlchemyError("connection reset"))
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    # Ne doit PAS raise.
    await tracker.record_ai_call(
        user_id=None,
        session_id=None,
        trace_id=None,
        expert_id="general",
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=0.0,
        outcome="completed",
    )


@pytest.mark.asyncio
async def test_record_ai_call_propagates_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession(raise_on_first_execute=asyncio.CancelledError())
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    with pytest.raises(asyncio.CancelledError):
        await tracker.record_ai_call(
            user_id=None,
            session_id=None,
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


@pytest.mark.asyncio
async def test_record_ai_call_extra_is_jsonified_and_passed_as_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    await tracker.record_ai_call(
        user_id=None,
        session_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=0.0001,
        outcome="completed",
        extra={"a": 1, "b": "x"},
    )

    # Le premier execute est l'INSERT ai_calls — `extra` sérialisé.
    _, params = session.executes[0]
    assert isinstance(params["extra"], str)
    assert json.loads(params["extra"]) == {"a": 1, "b": "x"}


@pytest.mark.asyncio
async def test_record_ai_call_converts_float_cost_to_decimal_in_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_fake_session(monkeypatch, session)

    tracker = CostTracker()
    await tracker.record_ai_call(
        user_id=None,
        session_id=None,
        trace_id=None,
        expert_id="general",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cost_usd=0.000123,
        outcome="completed",
    )

    _, insert_params = session.executes[0]
    assert isinstance(insert_params["cost_usd"], Decimal)
    assert insert_params["cost_usd"] == Decimal("0.000123")
