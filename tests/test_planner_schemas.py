"""Tests unitaires — validators Pydantic Planner (F1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.features.planner.schemas import (
    TaskCreate,
)


def _future() -> datetime:
    return datetime.now(tz=UTC) + timedelta(hours=2)


def _past() -> datetime:
    return datetime.now(tz=UTC) - timedelta(hours=1)


def test_task_create_once_accepts_future_at() -> None:
    body = TaskCreate(
        title="Morning briefing",
        prompt="Résume-moi l'actu",
        schedule={"type": "once", "at": _future().isoformat()},
    )
    assert body.schedule.type == "once"


def test_task_create_once_rejects_past_at() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "once", "at": _past().isoformat()},
        )


def test_task_create_interval_minutes_respects_min(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "tasks_min_interval_minutes", 5, raising=False)
    # 1 < min → rejet.
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "interval_minutes", "minutes": 1},
        )


def test_task_create_interval_minutes_valid() -> None:
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={"type": "interval_minutes", "minutes": 30},
    )
    assert body.schedule.minutes == 30


def test_task_create_daily_valid() -> None:
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={"type": "daily", "hour": 9, "minute": 30},
    )
    assert body.schedule.hour == 9


def test_task_create_daily_rejects_invalid_hour() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "daily", "hour": 25, "minute": 0},
        )


def test_task_create_weekly_valid() -> None:
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={
            "type": "weekly",
            "weekday": 0,
            "hour": 9,
            "minute": 0,
        },
    )
    assert body.schedule.weekday == 0


def test_task_create_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="",
            prompt="p",
            schedule={"type": "daily", "hour": 9, "minute": 0},
        )


def test_task_create_rejects_prompt_over_4000() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="x" * 4001,
            schedule={"type": "daily", "hour": 9, "minute": 0},
        )


def test_task_create_strips_title_and_prompt() -> None:
    body = TaskCreate(
        title="  morning  ",
        prompt="  résume  ",
        schedule={"type": "daily", "hour": 9, "minute": 0},
    )
    assert body.title == "morning"
    assert body.prompt == "résume"
