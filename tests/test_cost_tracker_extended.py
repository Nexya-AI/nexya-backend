"""
Tests N2 — couverture complémentaire `app.ai.cost_tracker.CostTracker`.

`tests/test_cost_tracker.py` couvre déjà les cas centraux (helpers, INSERT,
UPSERT, IntegrityError, fail-safe global). Ce fichier ajoute des cas qui
n'étaient pas testés :
1. `attempts > 1` et `fallback_used=True` correctement forwardés en SQL.
2. `user_id=None` accepté (bucket anonyme) — propagé dans les params.
3. `_jsonify` avec dict imbriqué et clés non-str.
4. `_to_decimal` avec Decimal → identité (pas de re-conversion).
5. UPSERT déclenché pour outcome `cancelled` (en plus de `completed`).
6. UPSERT pas déclenché pour outcome `failed`.
7. `record_ai_call_background` retourne bien une `asyncio.Task`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai import cost_tracker as cost_tracker_module
from app.ai.cost_tracker import CostTracker, _jsonify, _to_decimal


class _FakeAsyncSession:
    def __init__(self) -> None:
        self.executes: list[tuple[Any, dict[str, Any]]] = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.executes.append((stmt, params or {}))
        return MagicMock()

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _FakeAsyncSession) -> None:
    monkeypatch.setattr(cost_tracker_module, "AsyncSessionLocal", lambda: session)


def _kwargs(**overrides: Any) -> dict[str, Any]:
    """Kwargs de base pour record_ai_call — overridable par test."""
    return {
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "trace_id": "trace-abc",
        "expert_id": "general",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": 0.0001,
        "outcome": "completed",
        **overrides,
    }


# ══════════════════════════════════════════════════════════════
# 1. attempts + fallback_used
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_forwards_attempts_and_fallback_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_session(monkeypatch, session)
    tracker = CostTracker()

    await tracker.record_ai_call(**_kwargs(attempts=3, fallback_used=True))

    # Premier execute = INSERT ai_calls
    insert_params = session.executes[0][1]
    assert insert_params["attempts"] == 3
    assert insert_params["fallback_used"] is True


# ══════════════════════════════════════════════════════════════
# 2. user_id=None
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_with_user_id_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tests / batch / RGPD purgé : user_id=None doit passer sans crash."""
    session = _FakeAsyncSession()
    _install_session(monkeypatch, session)
    tracker = CostTracker()

    await tracker.record_ai_call(**_kwargs(user_id=None))

    insert_params = session.executes[0][1]
    assert insert_params["user_id"] is None
    # UPSERT usage_daily quand même appelé (bucket anonyme)
    assert len(session.executes) == 2
    assert session.commit_calls == 1


# ══════════════════════════════════════════════════════════════
# 3. _jsonify — cas avancés
# ══════════════════════════════════════════════════════════════


def test_jsonify_serializes_nested_dict() -> None:
    raw = {"a": 1, "b": {"c": [1, 2, 3], "d": "ok"}}
    out = _jsonify(raw)
    assert out is not None
    parsed = json.loads(out)
    assert parsed == raw


def test_jsonify_uses_default_str_for_non_serializable_values() -> None:
    """`json.dumps(default=str)` : un UUID ou un Decimal sont sérialisés
    via str() au lieu de raise."""
    uid = uuid.uuid4()
    out = _jsonify({"uid": uid, "amount": Decimal("0.0001")})
    assert out is not None
    parsed = json.loads(out)
    assert parsed["uid"] == str(uid)
    assert parsed["amount"] == "0.0001"


def test_jsonify_none_returns_none_not_string_null() -> None:
    """Subtilité : `_jsonify(None)` doit retourner None Python (pas la
    chaîne "null") pour que le bind SQL passe NULL et non un JSONB texte."""
    assert _jsonify(None) is None


# ══════════════════════════════════════════════════════════════
# 4. _to_decimal — Decimal en entrée = identité
# ══════════════════════════════════════════════════════════════


def test_to_decimal_with_decimal_input_is_identity() -> None:
    """Pas de re-conversion str() qui altérerait la précision."""
    src = Decimal("0.0000123456")
    out = _to_decimal(src)
    assert out is src  # même objet, pas une copie


def test_to_decimal_with_zero_returns_zero_decimal() -> None:
    assert _to_decimal(0) == Decimal("0")
    assert _to_decimal(0.0) == Decimal("0")


# ══════════════════════════════════════════════════════════════
# 5. UPSERT déclenché pour cancelled
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_cancelled_outcome_triggers_upsert_usage_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un stream annulé par l'user (cancelled) facture quand même les
    tokens déjà consommés — l'UPSERT usage_daily doit être appelé."""
    session = _FakeAsyncSession()
    _install_session(monkeypatch, session)
    tracker = CostTracker()

    await tracker.record_ai_call(**_kwargs(outcome="cancelled"))

    # 2 execute : INSERT ai_calls + UPSERT usage_daily
    assert len(session.executes) == 2
    assert session.commit_calls == 1


# ══════════════════════════════════════════════════════════════
# 6. UPSERT pas déclenché pour failed
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_failed_outcome_skips_upsert_usage_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un appel `failed` ne facture rien (rien produit côté user) — seul
    l'INSERT forensic ai_calls est exécuté, pas l'UPSERT."""
    session = _FakeAsyncSession()
    _install_session(monkeypatch, session)
    tracker = CostTracker()

    await tracker.record_ai_call(**_kwargs(outcome="failed", failure_code="LLM_UNAVAILABLE"))

    # Seul INSERT ai_calls — pas d'UPSERT usage_daily
    assert len(session.executes) == 1
    assert session.commit_calls == 1


# ══════════════════════════════════════════════════════════════
# 7. record_ai_call_background — retourne une asyncio.Task
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_record_ai_call_background_returns_asyncio_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeAsyncSession()
    _install_session(monkeypatch, session)
    tracker = CostTracker()

    task = tracker.record_ai_call_background(**_kwargs())
    assert isinstance(task, asyncio.Task)

    # On attend la complétion pour vérifier la persistence asynchrone
    await task

    assert session.commit_calls == 1
    assert len(session.executes) == 2  # INSERT + UPSERT
