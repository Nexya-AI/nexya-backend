"""Tests unitaires — `compute_next_run` helper (F1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.features.planner.scheduler import compute_next_run

_FIXED_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def test_once_future_returns_at() -> None:
    future_at = (_FIXED_NOW + timedelta(hours=2)).isoformat()
    result = compute_next_run("once", {"at": future_at}, from_dt=_FIXED_NOW)
    assert result is not None
    assert result == datetime.fromisoformat(future_at).astimezone(UTC)


def test_once_past_returns_none() -> None:
    past_at = (_FIXED_NOW - timedelta(hours=1)).isoformat()
    result = compute_next_run("once", {"at": past_at}, from_dt=_FIXED_NOW)
    assert result is None


def test_interval_minutes_returns_from_plus_minutes() -> None:
    result = compute_next_run("interval_minutes", {"minutes": 5}, from_dt=_FIXED_NOW)
    assert result == _FIXED_NOW + timedelta(minutes=5)


def test_interval_minutes_60() -> None:
    result = compute_next_run("interval_minutes", {"minutes": 60}, from_dt=_FIXED_NOW)
    assert result == _FIXED_NOW + timedelta(hours=1)


def test_interval_minutes_zero_or_negative_returns_none() -> None:
    assert compute_next_run("interval_minutes", {"minutes": 0}, from_dt=_FIXED_NOW) is None


def test_daily_future_today_returns_today_at_hhmm() -> None:
    # Now = 12:00, cible = 14:30 → aujourd'hui 14:30.
    result = compute_next_run("daily", {"hour": 14, "minute": 30}, from_dt=_FIXED_NOW)
    assert result == _FIXED_NOW.replace(hour=14, minute=30, second=0, microsecond=0)


def test_daily_past_today_returns_tomorrow_at_hhmm() -> None:
    # Now = 12:00, cible = 09:00 → demain 09:00.
    result = compute_next_run("daily", {"hour": 9, "minute": 0}, from_dt=_FIXED_NOW)
    assert result == (_FIXED_NOW + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


def test_weekly_future_returns_target_weekday() -> None:
    # 2026-04-24 = vendredi (weekday=4). Cible = samedi (weekday=5) 10:00.
    # Now = 12:00 vendredi, target = samedi → dans 1 jour.
    result = compute_next_run(
        "weekly",
        {"weekday": 5, "hour": 10, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == (_FIXED_NOW + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )


def test_weekly_same_day_past_returns_next_week() -> None:
    # Now = vendredi 12:00. Cible = vendredi 09:00 → vendredi prochain.
    result = compute_next_run(
        "weekly",
        {"weekday": 4, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == (_FIXED_NOW + timedelta(days=7)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )


def test_invalid_schedule_type_returns_none() -> None:
    assert compute_next_run("cron", {"expr": "0 9 * * MON"}) is None
    assert compute_next_run("", {}) is None
