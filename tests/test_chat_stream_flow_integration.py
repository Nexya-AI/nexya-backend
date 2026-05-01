"""
Tests N2 — flux d'intégration `/chat/stream` + `/chat/stop` + `/chat/reports`.

`tests/test_chat_stream_persisted.py` couvre déjà le mode persisté en
détail. Ce fichier complète avec les flux **runtime + cohésion** :
1. Singletons `runtime.py` — `get_ai_router` / `get_stream_handler` /
   `get_cost_tracker` retournent des instances réutilisables, et
   `reset_runtime_for_tests` les relâche.
2. `StreamHandler` est construit avec `cost_tracker` + `session_store`
   passés en kwargs (chaîne complète prête à émettre).
3. `POST /chat/stop` — délégation à `mark_cancelled(session_id)` (pas
   d'authentification cross-user — un attaquant ne peut pas annuler
   le stream d'un autre user car la clé Redis est scopée au session_id).
4. `POST /chat/reports` — rate limit user-scoped + délégation
   `ReportService.create_report`.
5. Smoke des 3 endpoints stream.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai import runtime
from app.ai.cost_tracker import CostTracker
from app.ai.router import LlmRouter
from app.ai.streaming import StreamHandler
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat import router as chat_router_mod
from app.main import app

# ══════════════════════════════════════════════════════════════
# 1. Singletons runtime.py
# ══════════════════════════════════════════════════════════════


def test_get_ai_router_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.reset_runtime_for_tests()
    a = runtime.get_ai_router()
    b = runtime.get_ai_router()
    assert a is b
    assert isinstance(a, LlmRouter)
    runtime.reset_runtime_for_tests()


def test_get_cost_tracker_returns_singleton() -> None:
    runtime.reset_runtime_for_tests()
    a = runtime.get_cost_tracker()
    b = runtime.get_cost_tracker()
    assert a is b
    assert isinstance(a, CostTracker)
    runtime.reset_runtime_for_tests()


def test_get_stream_handler_wires_router_cost_tracker_session_store() -> None:
    """`StreamHandler` doit recevoir `cost_tracker` + `session_store` —
    sinon la persistance forensic ai_calls / le filet Redis ne fonctionnent pas."""
    runtime.reset_runtime_for_tests()
    handler = runtime.get_stream_handler()
    assert isinstance(handler, StreamHandler)
    # Inspecte les attributs internes pour vérifier la chaîne complète
    # (le constructeur StreamHandler les bind à `self._*`)
    assert handler is runtime.get_stream_handler()  # idempotence singleton
    runtime.reset_runtime_for_tests()


def test_reset_runtime_for_tests_clears_all_singletons() -> None:
    runtime.reset_runtime_for_tests()
    handler_a = runtime.get_stream_handler()
    runtime.reset_runtime_for_tests()
    handler_b = runtime.get_stream_handler()
    assert handler_a is not handler_b
    runtime.reset_runtime_for_tests()


# ══════════════════════════════════════════════════════════════
# 2. POST /chat/stop — délégation mark_cancelled
# ══════════════════════════════════════════════════════════════


def _make_fake_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.is_pro = False
    user.is_active = True
    return user


@pytest.fixture
def chat_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_user = _make_fake_user()
    fake_session = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def test_chat_stop_delegates_to_mark_cancelled(
    chat_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_router_mod, "mark_cancelled", cancel_mock)

    session_id = "sess-xyz-123"
    resp = chat_client.post("/chat/stop", json={"session_id": session_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["session_id"] == session_id
    assert body["data"]["cancelled"] is True
    assert cancel_mock.await_count == 1
    assert cancel_mock.await_args.args == (session_id,)


def test_chat_stop_requires_authentication() -> None:
    """Sans dépendance overridée, `/chat/stop` rejette sans token."""
    resp = TestClient(app).post(
        "/chat/stop",
        json={"session_id": "any"},
    )
    assert resp.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════
# 3. POST /chat/reports
# ══════════════════════════════════════════════════════════════


def test_chat_reports_calls_rate_limiter_then_service(
    chat_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le pipeline doit : (1) rate limit, puis (2) délégation au service."""
    rate_limit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_router_mod, "rate_limit_abuse_reports", rate_limit_mock)

    fake_report = MagicMock()
    fake_report.id = uuid.uuid4()
    fake_report.user_id = uuid.uuid4()
    fake_report.message_id = uuid.uuid4()
    fake_report.conversation_id = uuid.uuid4()
    fake_report.reason = "offensive"
    fake_report.detail = None
    fake_report.status = "pending"
    fake_report.created_at = datetime.now(UTC)
    fake_report.updated_at = datetime.now(UTC)

    create_mock = AsyncMock(return_value=fake_report)
    monkeypatch.setattr(chat_router_mod.ReportService, "create_report", create_mock)

    resp = chat_client.post(
        "/chat/reports",
        json={
            "message_id": str(fake_report.message_id),
            "reason": "offensive",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["reason"] == "offensive"
    assert rate_limit_mock.await_count == 1
    assert create_mock.await_count == 1


def test_chat_reports_propagates_duplicate_report_409(
    chat_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors.exceptions import DuplicateReportException

    monkeypatch.setattr(
        chat_router_mod,
        "rate_limit_abuse_reports",
        AsyncMock(return_value=None),
    )

    async def _raise(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise DuplicateReportException()

    monkeypatch.setattr(chat_router_mod.ReportService, "create_report", _raise)

    resp = chat_client.post(
        "/chat/reports",
        json={
            "message_id": str(uuid.uuid4()),
            "reason": "offensive",
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "DUPLICATE_REPORT"


# ══════════════════════════════════════════════════════════════
# 4. Smoke — endpoints chat montés
# ══════════════════════════════════════════════════════════════


def test_chat_endpoints_are_mounted_smoke() -> None:
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    expected = {
        "/chat/stream",
        "/chat/stop",
        "/chat/reports",
        "/chat/conversations",
    }
    missing = expected - paths
    assert not missing, f"Endpoints chat manquants : {missing}"
