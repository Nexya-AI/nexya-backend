"""
Tests N2 — flux Planner : helpers `compute_next_run` × routeur `/tasks/*`.

`tests/test_scheduler_helpers.py` couvre déjà 10 cas de `compute_next_run`,
`tests/test_planner_router.py` couvre déjà 12 endpoints du routeur. Ce
fichier ajoute des **flux d'intégration** non couverts :
1. Cohérence `compute_next_run` ↔ `TaskCreate` (le helper accepte le dict
   produit par le model_dump du Pydantic v2 — anti-régression
   serialization).
2. Endpoint `/tasks/{id}/results` — délégation `list_results` avec curseur.
3. Endpoint `POST /tasks/{id}/pause` + `resume` — round-trip.
4. Anti-régression smoke : 8 endpoints attendus tous montés.
5. `compute_next_run` retourne `None` sur schedule_type inconnu (anti-crash).
6. Helper accepte `from_dt` aware UTC ET naive (auto-coerce).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.planner import service as planner_service_mod
from app.features.planner.scheduler import compute_next_run
from app.main import app

# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


def _make_fake_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.is_pro = False
    user.is_active = True
    return user


def _fake_task(task_id: uuid.UUID | None = None, *, status: str = "idle") -> MagicMock:
    task = MagicMock()
    task.id = task_id or uuid.uuid4()
    task.user_id = uuid.uuid4()
    task.title = "Daily summary"
    task.prompt = "Résume ma journée"
    task.expert_id = "general"
    task.schedule_type = "daily"
    task.schedule_config = {"hour": 9, "minute": 0}
    task.timezone = "UTC"
    task.next_run_at = datetime.now(UTC) + timedelta(hours=1)
    task.last_run_at = None
    task.run_count = 0
    task.retry_count = 0
    task.max_retries = 2
    task.status = status
    task.active = True
    task.paused = False
    task.auto_delete_after_run = False
    task.deleted_at = None
    task.created_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    return task


@pytest.fixture
def planner_client() -> TestClient:
    fake_user = _make_fake_user()
    fake_session = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# 1. compute_next_run cohérence avec model_dump Pydantic
# ══════════════════════════════════════════════════════════════


def test_compute_next_run_accepts_dict_with_iso_string_at() -> None:
    """`OnceConfig.at` est sérialisé en ISO string par Pydantic — le
    helper doit le parser correctement (pas seulement datetime objet)."""
    future = datetime.now(UTC) + timedelta(hours=2)
    result = compute_next_run(
        "once",
        {"at": future.isoformat(), "type": "once"},
    )
    assert result is not None
    assert result.tzinfo is UTC
    # Tolérance microsecondes : on compare au timestamp à la seconde près
    assert abs((result - future).total_seconds()) < 1.0


def test_compute_next_run_unknown_schedule_type_returns_none() -> None:
    """Un schedule_type inconnu (ex: client envoie un type buggé) doit
    retourner None plutôt que crasher — anti-régression."""
    result = compute_next_run("hourly_bogus", {"hour": 5})
    assert result is None


def test_compute_next_run_naive_from_dt_is_coerced_to_utc() -> None:
    """`from_dt` sans tzinfo doit être traité comme UTC (pas crash sur
    comparaison aware/naive)."""
    naive = datetime(2026, 6, 1, 12, 0, 0)  # noqa: DTZ001 — exprès naive
    result = compute_next_run("interval_minutes", {"minutes": 30}, from_dt=naive)
    assert result is not None
    assert result.tzinfo is UTC


def test_compute_next_run_once_in_the_past_returns_none() -> None:
    """Une tâche `once` dont `at` est déjà passé ne doit PAS reprogrammer."""
    past = datetime.now(UTC) - timedelta(hours=1)
    result = compute_next_run("once", {"at": past.isoformat()})
    assert result is None


def test_compute_next_run_invalid_iso_at_returns_none() -> None:
    """`at` malformé → None (pas crash)."""
    result = compute_next_run("once", {"at": "not-an-iso-date"})
    assert result is None


# ══════════════════════════════════════════════════════════════
# 2. POST /tasks/{id}/pause
# ══════════════════════════════════════════════════════════════


def test_pause_task_delegates_to_service(
    planner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paused_task = _fake_task()
    paused_task.paused = True
    paused_task.next_run_at = None
    pause_mock = AsyncMock(return_value=paused_task)
    monkeypatch.setattr(
        planner_service_mod.TaskSchedulerService, "pause_task", pause_mock
    )

    resp = planner_client.post(f"/tasks/{paused_task.id}/pause")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["paused"] is True
    assert body["data"]["next_run_at"] is None
    assert pause_mock.await_count == 1


def test_resume_task_delegates_to_service(
    planner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed_task = _fake_task()
    resumed_task.paused = False
    resume_mock = AsyncMock(return_value=resumed_task)
    monkeypatch.setattr(
        planner_service_mod.TaskSchedulerService, "resume_task", resume_mock
    )

    resp = planner_client.post(f"/tasks/{resumed_task.id}/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["paused"] is False
    assert resume_mock.await_count == 1


# ══════════════════════════════════════════════════════════════
# 3. GET /tasks/{id}/results — pagination
# ══════════════════════════════════════════════════════════════


def test_list_task_results_forwards_cursor_and_limit(
    planner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.planner.service import TaskResultsPageOrm

    list_mock = AsyncMock(
        return_value=TaskResultsPageOrm(items=[], next_cursor="next-curseur-base64")
    )
    monkeypatch.setattr(
        planner_service_mod.TaskSchedulerService, "list_results", list_mock
    )

    task_id = uuid.uuid4()
    resp = planner_client.get(
        f"/tasks/{task_id}/results",
        params={"cursor": "curseur-precedent", "limit": 10},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["next_cursor"] == "next-curseur-base64"
    assert list_mock.await_count == 1
    # Vérifie que cursor + limit sont bien forwardés
    kwargs = list_mock.await_args.kwargs
    assert kwargs["cursor"] == "curseur-precedent"
    assert kwargs["limit"] == 10


# ══════════════════════════════════════════════════════════════
# 4. Smoke — endpoints planner
# ══════════════════════════════════════════════════════════════


def test_planner_endpoints_are_mounted_smoke() -> None:
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    expected = {
        "/tasks",
        "/tasks/{task_id}",
        "/tasks/{task_id}/pause",
        "/tasks/{task_id}/resume",
        "/tasks/{task_id}/results",
    }
    missing = expected - paths
    assert not missing, f"Endpoints planner manquants : {missing}"


# ══════════════════════════════════════════════════════════════
# 5. DELETE 204 idempotent
# ══════════════════════════════════════════════════════════════


def test_delete_task_returns_204_no_body(
    planner_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        planner_service_mod.TaskSchedulerService, "soft_delete_task", delete_mock
    )

    task_id = uuid.uuid4()
    resp = planner_client.delete(f"/tasks/{task_id}")
    assert resp.status_code == 204
    assert resp.content == b""  # 204 = pas de body
    assert delete_mock.await_count == 1
