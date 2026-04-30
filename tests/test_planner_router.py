"""Tests d'intégration — router `/tasks/*` (F1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    ResourceNotFoundException,
    TasksQuotaExceededException,
)
from app.features.auth.models import User
from app.features.planner.models import ScheduledTask
from app.features.planner.service import (
    TaskResultsPageOrm,
    TaskSchedulerService,
    TasksPageOrm,
)
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_task() -> ScheduledTask:
    task = ScheduledTask(
        user_id=_FAKE_USER_ID,
        title="Morning",
        prompt="test",
        expert_id="general",
        schedule_type="daily",
        schedule_config={"hour": 9, "minute": 0},
        timezone="UTC",
        next_run_at=_NOW + timedelta(hours=1),
        last_run_at=None,
        status="idle",
        active=True,
        paused=False,
        auto_delete_after_run=False,
        retry_count=0,
        max_retries=2,
        run_count=0,
    )
    task.id = uuid.uuid4()
    task.created_at = _NOW
    task.updated_at = _NOW
    task.deleted_at = None
    task.metadata_json = None
    return task


@pytest.fixture
def client() -> TestClient:
    fake_user = _make_user()
    fake_db = MagicMock()

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _valid_body(schedule_type: str = "daily") -> dict:
    if schedule_type == "once":
        return {
            "title": "T",
            "prompt": "p",
            "schedule": {
                "type": "once",
                "at": (_NOW + timedelta(hours=3)).isoformat(),
            },
        }
    return {
        "title": "T",
        "prompt": "p",
        "schedule": {"type": "daily", "hour": 9, "minute": 0},
    }


# ══════════════════════════════════════════════════════════════
# POST /tasks
# ══════════════════════════════════════════════════════════════


def test_post_tasks_201(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    monkeypatch.setattr(
        TaskSchedulerService,
        "create_task",
        AsyncMock(return_value=task),
    )
    response = client.post("/tasks", json=_valid_body())
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["title"] == "Morning"


def test_post_tasks_402_quota_exceeded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TaskSchedulerService,
        "create_task",
        AsyncMock(side_effect=TasksQuotaExceededException(current=3, maximum=3, plan="free")),
    )
    response = client.post("/tasks", json=_valid_body())
    assert response.status_code == 402
    assert response.json()["code"] == "TASKS_QUOTA_EXCEEDED"
    assert response.json()["data"]["plan"] == "free"


def test_post_tasks_422_on_empty_title(client: TestClient) -> None:
    body = _valid_body()
    body["title"] = ""
    response = client.post("/tasks", json=body)
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# GET /tasks + GET /{id}
# ══════════════════════════════════════════════════════════════


def test_get_tasks_200_with_pagination(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    monkeypatch.setattr(
        TaskSchedulerService,
        "list_for_user",
        AsyncMock(return_value=TasksPageOrm(items=[task], next_cursor="next-opaque")),
    )
    response = client.get("/tasks?limit=10")
    assert response.status_code == 200
    assert response.json()["data"]["next_cursor"] == "next-opaque"
    assert len(response.json()["data"]["items"]) == 1


def test_get_tasks_filter_status_forwarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_list = AsyncMock(return_value=TasksPageOrm(items=[], next_cursor=None))
    monkeypatch.setattr(TaskSchedulerService, "list_for_user", mock_list)
    response = client.get("/tasks?status=failed")
    assert response.status_code == 200
    assert mock_list.await_args.kwargs["status"] == "failed"


def test_get_task_404_on_idor(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TaskSchedulerService,
        "get_task",
        AsyncMock(side_effect=ResourceNotFoundException("Tâche")),
    )
    response = client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code == 404


# ══════════════════════════════════════════════════════════════
# PATCH
# ══════════════════════════════════════════════════════════════


def test_patch_task_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    monkeypatch.setattr(
        TaskSchedulerService,
        "update_task",
        AsyncMock(return_value=task),
    )
    response = client.patch(f"/tasks/{task.id}", json={"title": "Nouveau titre"})
    assert response.status_code == 200


def test_patch_task_422_no_field(client: TestClient) -> None:
    response = client.patch(f"/tasks/{uuid.uuid4()}", json={})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# DELETE / PAUSE / RESUME
# ══════════════════════════════════════════════════════════════


def test_delete_task_204(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TaskSchedulerService,
        "soft_delete_task",
        AsyncMock(return_value=None),
    )
    response = client.delete(f"/tasks/{uuid.uuid4()}")
    assert response.status_code == 204


def test_post_pause_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    task.paused = True
    task.status = "paused"
    monkeypatch.setattr(TaskSchedulerService, "pause_task", AsyncMock(return_value=task))
    response = client.post(f"/tasks/{task.id}/pause")
    assert response.status_code == 200
    assert response.json()["data"]["paused"] is True


def test_post_resume_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    monkeypatch.setattr(TaskSchedulerService, "resume_task", AsyncMock(return_value=task))
    response = client.post(f"/tasks/{task.id}/resume")
    assert response.status_code == 200
    assert response.json()["data"]["paused"] is False


# ══════════════════════════════════════════════════════════════
# GET /tasks/{id}/results
# ══════════════════════════════════════════════════════════════


def test_get_task_results_200(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TaskSchedulerService,
        "list_results",
        AsyncMock(return_value=TaskResultsPageOrm(items=[], next_cursor=None)),
    )
    response = client.get(f"/tasks/{uuid.uuid4()}/results")
    assert response.status_code == 200
    assert response.json()["data"]["items"] == []
