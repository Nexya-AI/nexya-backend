"""
Tests F3 — Migration du hook post-exécution de task planifiée.

Le hook F2 `_send_task_push_notification` a été SUPPRIMÉ et remplacé
par `_dispatch_task_notification` qui délègue à `NotificationDispatcher`.

Ce fichier remplace `test_scheduler_push_hook.py` F2 (même nom pour
préserver la continuité de l'historique CI) et vérifie la nouvelle
architecture F3 :

- `_dispatch_task_notification` charge task + user + délègue au dispatcher.
- Skip silencieux si task deleted ou user purgé.
- `category='tasks'` + `source_kind='scheduled_task'` + `source_task_id`
  transmis proprement.
- Payload `data` contient `task_id`, `status`, `deep_link`, `task_title`,
  `notification_kind='completed'`.
- Body tronqué à `fcm_body_preview_max_chars`.
- Body spécifique selon result_status (success / skipped / failed).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from workers.scheduler_tasks import (
    _build_task_notification_body,
    _dispatch_task_notification,
)


def _make_task(
    *,
    task_id: uuid.UUID | None = None,
    title: str = "Ma tâche",
    user_id: uuid.UUID | None = None,
    deleted_at=None,
):
    task = MagicMock()
    task.id = task_id or uuid.uuid4()
    task.title = title
    task.user_id = user_id or uuid.uuid4()
    task.deleted_at = deleted_at
    return task


def _make_user(user_id: uuid.UUID | None = None):
    u = MagicMock()
    u.id = user_id or uuid.uuid4()
    u.email = "ivan@nexya.ai"
    u.display_name = "Ivan"
    u.username = "ivan"
    return u


# ═══════════════════════════════════════════════════════════════════
# _build_task_notification_body
# ═══════════════════════════════════════════════════════════════════


def test_build_body_success_returns_result_text():
    body = _build_task_notification_body("success", "Voici le résultat")
    assert body == "Voici le résultat"


def test_build_body_success_truncates_long_text(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "fcm_body_preview_max_chars", 20)
    body = _build_task_notification_body("success", "A" * 500)
    assert len(body) <= 20
    assert body.endswith("…")


def test_build_body_success_empty_falls_back():
    body = _build_task_notification_body("success", "")
    assert body == "Tâche exécutée."


def test_build_body_skipped_message():
    body = _build_task_notification_body("skipped", None)
    assert "quota" in body.lower()


def test_build_body_failed_message():
    body = _build_task_notification_body("failed", None)
    assert "Échec" in body


# ═══════════════════════════════════════════════════════════════════
# _dispatch_task_notification
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_delegates_to_notification_dispatcher(monkeypatch):
    task = _make_task(title="Prendre médicament")
    user = _make_user(user_id=task.user_id)

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=None)

    # db.get alternates : 1er call → task, 2e call → user
    async def _get(model, pk):
        if "ScheduledTask" in repr(model):
            return task
        return user

    fake_db.get = AsyncMock(side_effect=_get)

    monkeypatch.setattr(
        "workers.scheduler_tasks.AsyncSessionLocal",
        lambda: fake_db,
    )

    dispatch_mock = AsyncMock()
    monkeypatch.setattr(
        "workers.scheduler_tasks.NotificationDispatcher.dispatch",
        dispatch_mock,
    )

    await _dispatch_task_notification(
        task_uuid=task.id,
        result_status="success",
        result_text="Résultat de la tâche",
    )

    dispatch_mock.assert_awaited_once()
    kwargs = dispatch_mock.await_args.kwargs
    assert kwargs["category"] == "tasks"
    assert kwargs["source_kind"] == "scheduled_task"
    assert kwargs["source_task_id"] == task.id
    assert kwargs["user"] is user
    assert kwargs["data"]["task_id"] == str(task.id)
    assert kwargs["data"]["status"] == "success"
    assert kwargs["data"]["deep_link"] == f"nexya://task/{task.id}"
    assert kwargs["data"]["task_title"] == "Prendre médicament"
    assert kwargs["data"]["notification_kind"] == "completed"


@pytest.mark.asyncio
async def test_dispatch_skips_if_task_gone(monkeypatch):
    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=None)
    fake_db.get = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "workers.scheduler_tasks.AsyncSessionLocal",
        lambda: fake_db,
    )

    dispatch_mock = AsyncMock()
    monkeypatch.setattr(
        "workers.scheduler_tasks.NotificationDispatcher.dispatch",
        dispatch_mock,
    )

    await _dispatch_task_notification(
        task_uuid=uuid.uuid4(),
        result_status="success",
        result_text="x",
    )
    dispatch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_skips_if_task_deleted(monkeypatch):
    task = _make_task(deleted_at="2026-04-25")
    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=None)
    fake_db.get = AsyncMock(return_value=task)

    monkeypatch.setattr(
        "workers.scheduler_tasks.AsyncSessionLocal",
        lambda: fake_db,
    )

    dispatch_mock = AsyncMock()
    monkeypatch.setattr(
        "workers.scheduler_tasks.NotificationDispatcher.dispatch",
        dispatch_mock,
    )

    await _dispatch_task_notification(
        task_uuid=task.id,
        result_status="success",
        result_text="x",
    )
    dispatch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_skips_if_user_missing(monkeypatch):
    task = _make_task()

    async def _get(model, pk):
        if "ScheduledTask" in repr(model):
            return task
        return None  # user purgé

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=None)
    fake_db.get = AsyncMock(side_effect=_get)

    monkeypatch.setattr(
        "workers.scheduler_tasks.AsyncSessionLocal",
        lambda: fake_db,
    )

    dispatch_mock = AsyncMock()
    monkeypatch.setattr(
        "workers.scheduler_tasks.NotificationDispatcher.dispatch",
        dispatch_mock,
    )

    await _dispatch_task_notification(
        task_uuid=task.id,
        result_status="success",
        result_text="x",
    )
    dispatch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failed_status_propagates_body(monkeypatch):
    task = _make_task()
    user = _make_user(user_id=task.user_id)

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=None)

    async def _get(model, pk):
        if "ScheduledTask" in repr(model):
            return task
        return user

    fake_db.get = AsyncMock(side_effect=_get)

    monkeypatch.setattr(
        "workers.scheduler_tasks.AsyncSessionLocal",
        lambda: fake_db,
    )

    dispatch_mock = AsyncMock()
    monkeypatch.setattr(
        "workers.scheduler_tasks.NotificationDispatcher.dispatch",
        dispatch_mock,
    )

    await _dispatch_task_notification(
        task_uuid=task.id,
        result_status="failed",
        result_text=None,
    )
    assert dispatch_mock.await_args.kwargs["data"]["status"] == "failed"
    assert "Échec" in dispatch_mock.await_args.kwargs["body"]
