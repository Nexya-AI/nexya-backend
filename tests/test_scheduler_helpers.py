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


# ══════════════════════════════════════════════════════════════
# Monthly (extension F0.5)
# ══════════════════════════════════════════════════════════════


def test_monthly_future_this_month_returns_this_month() -> None:
    # Now = 2026-04-24 12:00. Cible = jour 28 du mois à 09:00 → 2026-04-28 09:00.
    result = compute_next_run(
        "monthly",
        {"day": 28, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 28, 9, 0, 0, tzinfo=UTC)


def test_monthly_past_this_month_returns_next_month() -> None:
    # Now = 2026-04-24. Cible = jour 10 → mai (avril déjà passé).
    result = compute_next_run(
        "monthly",
        {"day": 10, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 5, 10, 9, 0, 0, tzinfo=UTC)


def test_monthly_clamps_day_31_in_april() -> None:
    # Now = 2026-04-24. Cible = jour 31. Avril a 30 jours → clamp à 30.
    result = compute_next_run(
        "monthly",
        {"day": 31, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 30, 9, 0, 0, tzinfo=UTC)


def test_monthly_clamps_day_31_in_february_non_leap() -> None:
    # Now = 2027-01-15 (2027 NON bissextile). Cible jour 31 → fév clamp à 28.
    base = datetime(2027, 1, 15, 12, 0, 0, tzinfo=UTC)
    # Jour 15 déjà passé en janvier 12:00 ? Non, on est le 15 12:00, cible 31 → 31 jan.
    # Forçons fév : on prend cible jour 31 le 1er fév.
    base_feb = datetime(2027, 2, 5, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly",
        {"day": 31, "hour": 9, "minute": 0},
        from_dt=base_feb,
    )
    assert result == datetime(2027, 2, 28, 9, 0, 0, tzinfo=UTC)
    _ = base  # référence à la base alternative


def test_monthly_clamps_day_31_in_february_leap() -> None:
    # 2028 = bissextile. Cible jour 31 en fév → 29.
    base = datetime(2028, 2, 5, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly",
        {"day": 31, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2028, 2, 29, 9, 0, 0, tzinfo=UTC)


def test_monthly_invalid_day_returns_none() -> None:
    assert compute_next_run("monthly", {"day": 0, "hour": 9, "minute": 0}) is None
    assert compute_next_run("monthly", {"day": 32, "hour": 9, "minute": 0}) is None


def test_monthly_december_rolls_to_january_next_year() -> None:
    # Now = 31 déc 2026 23:00. Cible jour 1 → 1er jan 2027.
    base = datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly",
        {"day": 1, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2027, 1, 1, 9, 0, 0, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════
# Yearly (extension F0.5)
# ══════════════════════════════════════════════════════════════


def test_yearly_future_this_year_returns_this_year() -> None:
    # Now = 2026-04-24. Cible = 21 juin → 2026-06-21.
    result = compute_next_run(
        "yearly",
        {"month": 6, "day": 21, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 6, 21, 9, 0, 0, tzinfo=UTC)


def test_yearly_past_this_year_returns_next_year() -> None:
    # Now = 2026-04-24. Cible = 1er fév → 2027-02-01.
    result = compute_next_run(
        "yearly",
        {"month": 2, "day": 1, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2027, 2, 1, 9, 0, 0, tzinfo=UTC)


def test_yearly_29_february_clamps_to_28_non_leap() -> None:
    # Now = 2027-01-15 (2027 NON bissextile). Cible 29 fév → 28 fév.
    base = datetime(2027, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "yearly",
        {"month": 2, "day": 29, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2027, 2, 28, 9, 0, 0, tzinfo=UTC)


def test_yearly_29_february_returns_29_on_leap_year() -> None:
    # 2028 = bissextile. Cible 29 fév → 29 fév.
    base = datetime(2028, 1, 15, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "yearly",
        {"month": 2, "day": 29, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2028, 2, 29, 9, 0, 0, tzinfo=UTC)


def test_yearly_invalid_month_returns_none() -> None:
    assert compute_next_run("yearly", {"month": 13, "day": 1, "hour": 9, "minute": 0}) is None
    assert compute_next_run("yearly", {"month": 0, "day": 1, "hour": 9, "minute": 0}) is None
