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


# ══════════════════════════════════════════════════════════════
# Monthly / Yearly (extension F0.5)
# ══════════════════════════════════════════════════════════════


def test_task_create_monthly_valid() -> None:
    body = TaskCreate(
        title="Bilan mensuel",
        prompt="Fais le bilan du mois",
        schedule={"type": "monthly", "day": 15, "hour": 9, "minute": 0},
    )
    assert body.schedule.type == "monthly"
    assert body.schedule.day == 15


def test_task_create_monthly_accepts_day_31() -> None:
    # day=31 est valide à la création (le clamp arrive au calcul next_run_at).
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={"type": "monthly", "day": 31, "hour": 9, "minute": 0},
    )
    assert body.schedule.day == 31


def test_task_create_monthly_rejects_day_zero() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "monthly", "day": 0, "hour": 9, "minute": 0},
        )


def test_task_create_monthly_rejects_day_over_31() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "monthly", "day": 32, "hour": 9, "minute": 0},
        )


def test_task_create_yearly_valid() -> None:
    body = TaskCreate(
        title="Anniversaire",
        prompt="Souhaite l'anniv",
        schedule={"type": "yearly", "month": 6, "day": 21, "hour": 9, "minute": 0},
    )
    assert body.schedule.type == "yearly"
    assert body.schedule.month == 6
    assert body.schedule.day == 21


def test_task_create_yearly_accepts_29_february() -> None:
    # 29 février valide à la création (clamp à 28 sur année non bissextile lors du calcul).
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={"type": "yearly", "month": 2, "day": 29, "hour": 9, "minute": 0},
    )
    assert body.schedule.day == 29


def test_task_create_yearly_rejects_30_february() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "yearly", "month": 2, "day": 30, "hour": 9, "minute": 0},
        )


def test_task_create_yearly_rejects_31_april() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "yearly", "month": 4, "day": 31, "hour": 9, "minute": 0},
        )


def test_task_create_yearly_rejects_invalid_month() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "yearly", "month": 13, "day": 1, "hour": 9, "minute": 0},
        )


# ══════════════════════════════════════════════════════════════
# WeeklyRange (F1.5 — range continu lundi→vendredi)
# ══════════════════════════════════════════════════════════════


def test_task_create_weekly_range_valid() -> None:
    body = TaskCreate(
        title="Résumé crypto jours ouvrés",
        prompt="Donne-moi le cours du Bitcoin",
        schedule={
            "type": "weekly_range",
            "start_weekday": 0,
            "end_weekday": 4,
            "hour": 8,
            "minute": 0,
        },
    )
    assert body.schedule.type == "weekly_range"
    assert body.schedule.start_weekday == 0
    assert body.schedule.end_weekday == 4


def test_task_create_weekly_range_accepts_weekend_wrap() -> None:
    # start > end : le range enjambe le weekend (samedi → mardi).
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={
            "type": "weekly_range",
            "start_weekday": 5,
            "end_weekday": 1,
            "hour": 10,
            "minute": 0,
        },
    )
    assert body.schedule.start_weekday == 5
    assert body.schedule.end_weekday == 1


def test_task_create_weekly_range_rejects_single_day() -> None:
    # start == end → un seul jour, on doit utiliser `weekly`.
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "weekly_range",
                "start_weekday": 2,
                "end_weekday": 2,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_weekly_range_rejects_weekday_out_of_bounds() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "weekly_range",
                "start_weekday": 7,
                "end_weekday": 4,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_weekly_range_rejects_invalid_hour() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "weekly_range",
                "start_weekday": 0,
                "end_weekday": 4,
                "hour": 24,
                "minute": 0,
            },
        )


# ══════════════════════════════════════════════════════════════
# MonthlyRange (F1.5 — range continu du 15 au 30 du mois)
# ══════════════════════════════════════════════════════════════


def test_task_create_monthly_range_valid() -> None:
    body = TaskCreate(
        title="Rappel loyer fin de mois",
        prompt="Rappelle-moi de payer le loyer",
        schedule={
            "type": "monthly_range",
            "start_day": 15,
            "end_day": 30,
            "hour": 9,
            "minute": 0,
        },
    )
    assert body.schedule.type == "monthly_range"
    assert body.schedule.start_day == 15
    assert body.schedule.end_day == 30


def test_task_create_monthly_range_rejects_start_after_end() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "monthly_range",
                "start_day": 20,
                "end_day": 10,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_monthly_range_rejects_single_day() -> None:
    # start == end → un seul jour, on doit utiliser `monthly`.
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "monthly_range",
                "start_day": 15,
                "end_day": 15,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_monthly_range_rejects_day_zero() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "monthly_range",
                "start_day": 0,
                "end_day": 15,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_monthly_range_rejects_day_over_31() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "monthly_range",
                "start_day": 15,
                "end_day": 32,
                "hour": 9,
                "minute": 0,
            },
        )


# ══════════════════════════════════════════════════════════════
# MultiWeekday (F1.5 — jours non-continus mardi + jeudi)
# ══════════════════════════════════════════════════════════════


def test_task_create_multi_weekday_valid() -> None:
    body = TaskCreate(
        title="Gym",
        prompt="Rappelle-moi la gym",
        schedule={
            "type": "multi_weekday",
            "weekdays": [1, 3],
            "hour": 18,
            "minute": 0,
        },
    )
    assert body.schedule.type == "multi_weekday"
    assert body.schedule.weekdays == [1, 3]


def test_task_create_multi_weekday_sorts_weekdays() -> None:
    # La liste est triée par le validator (idempotence DB).
    body = TaskCreate(
        title="Yoga",
        prompt="p",
        schedule={
            "type": "multi_weekday",
            "weekdays": [4, 0, 2],
            "hour": 7,
            "minute": 30,
        },
    )
    assert body.schedule.weekdays == [0, 2, 4]


def test_task_create_multi_weekday_rejects_single_day() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "multi_weekday", "weekdays": [1], "hour": 9, "minute": 0},
        )


def test_task_create_multi_weekday_rejects_empty() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={"type": "multi_weekday", "weekdays": [], "hour": 9, "minute": 0},
        )


def test_task_create_multi_weekday_rejects_duplicates() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "multi_weekday",
                "weekdays": [1, 1, 2],
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_multi_weekday_rejects_more_than_six() -> None:
    # 7 jours → utiliser `daily` (max_length=6).
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "multi_weekday",
                "weekdays": [0, 1, 2, 3, 4, 5, 6],
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_multi_weekday_rejects_weekday_out_of_bounds() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "multi_weekday",
                "weekdays": [1, 8],
                "hour": 9,
                "minute": 0,
            },
        )


# ══════════════════════════════════════════════════════════════
# YearlyRange (F1.6 — mois figé + range de jours, ex : janvier du 15 au 30)
# ══════════════════════════════════════════════════════════════


def test_task_create_yearly_range_valid() -> None:
    body = TaskCreate(
        title="Bilan janvier",
        prompt="Rappelle-moi le bilan",
        schedule={
            "type": "yearly_range",
            "month": 1,
            "start_day": 15,
            "end_day": 30,
            "hour": 9,
            "minute": 0,
        },
    )
    assert body.schedule.type == "yearly_range"
    assert body.schedule.month == 1
    assert body.schedule.start_day == 15
    assert body.schedule.end_day == 30


def test_task_create_yearly_range_accepts_29_february() -> None:
    # 29 février accepté à la création (clamp à 28 au calcul next_run_at).
    body = TaskCreate(
        title="T",
        prompt="p",
        schedule={
            "type": "yearly_range",
            "month": 2,
            "start_day": 10,
            "end_day": 29,
            "hour": 9,
            "minute": 0,
        },
    )
    assert body.schedule.end_day == 29


def test_task_create_yearly_range_rejects_30_february() -> None:
    # 30 février n'existe jamais → rejeté.
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "yearly_range",
                "month": 2,
                "start_day": 10,
                "end_day": 30,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_yearly_range_rejects_31_april() -> None:
    # Avril a 30 jours → end_day=31 rejeté.
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "yearly_range",
                "month": 4,
                "start_day": 10,
                "end_day": 31,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_yearly_range_rejects_start_day_invalid_for_month() -> None:
    # start_day=30 en février → rejeté (la borne basse aussi est validée).
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "yearly_range",
                "month": 2,
                "start_day": 30,
                "end_day": 31,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_yearly_range_rejects_start_after_end() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "yearly_range",
                "month": 6,
                "start_day": 20,
                "end_day": 10,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_yearly_range_rejects_single_day() -> None:
    # start == end → utiliser `yearly`.
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "yearly_range",
                "month": 6,
                "start_day": 15,
                "end_day": 15,
                "hour": 9,
                "minute": 0,
            },
        )


def test_task_create_yearly_range_rejects_invalid_month() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="T",
            prompt="p",
            schedule={
                "type": "yearly_range",
                "month": 13,
                "start_day": 10,
                "end_day": 20,
                "hour": 9,
                "minute": 0,
            },
        )
