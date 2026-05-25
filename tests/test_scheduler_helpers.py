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


# ══════════════════════════════════════════════════════════════
# WeeklyRange (extension F1.5 — lundi → vendredi, enjambement weekend)
# Repère : _FIXED_NOW = 2026-04-24 12:00 UTC = VENDREDI (weekday=4).
# ══════════════════════════════════════════════════════════════


def test_weekly_range_today_in_range_future_hour_returns_today() -> None:
    # Lun→ven (0-4), 14:00. Aujourd'hui vendredi 12:00 → aujourd'hui 14:00.
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 0, "end_weekday": 4, "hour": 14, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 24, 14, 0, 0, tzinfo=UTC)


def test_weekly_range_today_in_range_past_hour_returns_next_allowed_day() -> None:
    # Lun→ven (0-4), 09:00. Vendredi 09:00 déjà passé → saute sa/di → lundi.
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 0, "end_weekday": 4, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 27, 9, 0, 0, tzinfo=UTC)


def test_weekly_range_from_saturday_skips_to_monday() -> None:
    # Lun→ven (0-4) depuis un samedi → samedi/dimanche exclus → lundi.
    base_sat = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)  # samedi
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 0, "end_weekday": 4, "hour": 9, "minute": 0},
        from_dt=base_sat,
    )
    assert result == datetime(2026, 4, 27, 9, 0, 0, tzinfo=UTC)


def test_weekly_range_weekend_only_returns_saturday() -> None:
    # Sam→dim (5-6), 10:00. Aujourd'hui vendredi → samedi.
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 5, "end_weekday": 6, "hour": 10, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 25, 10, 0, 0, tzinfo=UTC)


def test_weekly_range_wrap_weekend_from_friday() -> None:
    # Sam→mar (5→1), enjambe le weekend → {sa,di,lu,ma}. Vendredi → samedi.
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 5, "end_weekday": 1, "hour": 10, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 25, 10, 0, 0, tzinfo=UTC)


def test_weekly_range_wrap_weekend_from_sunday_returns_today() -> None:
    # Sam→mar (5→1). Dimanche 08:00, cible 10:00 → aujourd'hui dimanche 10:00.
    base_sun = datetime(2026, 4, 26, 8, 0, 0, tzinfo=UTC)  # dimanche
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 5, "end_weekday": 1, "hour": 10, "minute": 0},
        from_dt=base_sun,
    )
    assert result == datetime(2026, 4, 26, 10, 0, 0, tzinfo=UTC)


def test_weekly_range_wrap_weekend_from_monday_past_hour_returns_tuesday() -> None:
    # Sam→mar (5→1). Lundi 15:00, cible 10:00 déjà passée → mardi.
    base_mon = datetime(2026, 4, 27, 15, 0, 0, tzinfo=UTC)  # lundi
    result = compute_next_run(
        "weekly_range",
        {"start_weekday": 5, "end_weekday": 1, "hour": 10, "minute": 0},
        from_dt=base_mon,
    )
    assert result == datetime(2026, 4, 28, 10, 0, 0, tzinfo=UTC)


def test_weekly_range_invalid_weekday_returns_none() -> None:
    assert (
        compute_next_run(
            "weekly_range",
            {"start_weekday": 7, "end_weekday": 4, "hour": 9, "minute": 0},
        )
        is None
    )


def test_weekly_range_single_day_returns_none() -> None:
    # start == end : compute_next_run est défensif (la config ne devrait
    # jamais arriver là vu le validator Pydantic, mais on protège).
    assert (
        compute_next_run(
            "weekly_range",
            {"start_weekday": 3, "end_weekday": 3, "hour": 9, "minute": 0},
        )
        is None
    )


def test_weekly_range_missing_field_returns_none() -> None:
    assert compute_next_run("weekly_range", {"start_weekday": 0}) is None
    assert compute_next_run("weekly_range", {}) is None


# ══════════════════════════════════════════════════════════════
# MonthlyRange (extension F1.5 — du 15 au 30 du mois, clamp dernier jour)
# ══════════════════════════════════════════════════════════════


def test_monthly_range_today_in_range_future_hour_returns_today() -> None:
    # 15→30, 14:00. Aujourd'hui 24 avril 12:00 → aujourd'hui 14:00.
    result = compute_next_run(
        "monthly_range",
        {"start_day": 15, "end_day": 30, "hour": 14, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 24, 14, 0, 0, tzinfo=UTC)


def test_monthly_range_today_in_range_past_hour_returns_next_day() -> None:
    # 15→30, 09:00. 24 avril 09:00 passé → jour suivant 25.
    result = compute_next_run(
        "monthly_range",
        {"start_day": 15, "end_day": 30, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 25, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_before_range_returns_start_day() -> None:
    # 26→30. Aujourd'hui 24 < 26 → 26 avril.
    result = compute_next_run(
        "monthly_range",
        {"start_day": 26, "end_day": 30, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 26, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_passed_this_month_returns_next_month() -> None:
    # 1→10. Aujourd'hui 24 > 10 → range passé → 1er mai.
    result = compute_next_run(
        "monthly_range",
        {"start_day": 1, "end_day": 10, "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_last_day_past_hour_rolls_to_next_month() -> None:
    # 15→30. Le 30 avril 13:00, cible 09:00 passée, pas de jour suivant
    # dans le range (31 > 30) → 15 mai.
    base = datetime(2026, 4, 30, 13, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly_range",
        {"start_day": 15, "end_day": 30, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2026, 5, 15, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_clamps_end_31_in_april() -> None:
    # 25→31. Avril a 30 jours → end clampé à 30. Aujourd'hui 10 < 25 → 25 avril.
    base = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly_range",
        {"start_day": 25, "end_day": 31, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2026, 4, 25, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_clamps_start_30_in_february_non_leap() -> None:
    # 30→31 en février 2027 (non bissextile, 28 jours). start ET end
    # clampés à 28. Aujourd'hui 5 < 28 → 28 février. (Sans le clamp de
    # `start`, `from_dt.replace(day=30)` lèverait une ValueError.)
    base = datetime(2027, 2, 5, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly_range",
        {"start_day": 30, "end_day": 31, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2027, 2, 28, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_clamps_start_30_in_february_leap() -> None:
    # 30→31 en février 2028 (bissextile, 29 jours) → clamp à 29.
    base = datetime(2028, 2, 5, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly_range",
        {"start_day": 30, "end_day": 31, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2028, 2, 29, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_december_rolls_to_january_next_year() -> None:
    # 1→5. Le 20 décembre 2026 > 5 → range passé → 1er janvier 2027.
    base = datetime(2026, 12, 20, 12, 0, 0, tzinfo=UTC)
    result = compute_next_run(
        "monthly_range",
        {"start_day": 1, "end_day": 5, "hour": 9, "minute": 0},
        from_dt=base,
    )
    assert result == datetime(2027, 1, 1, 9, 0, 0, tzinfo=UTC)


def test_monthly_range_rejects_start_after_end_returns_none() -> None:
    assert (
        compute_next_run(
            "monthly_range",
            {"start_day": 20, "end_day": 10, "hour": 9, "minute": 0},
        )
        is None
    )


def test_monthly_range_rejects_single_day_returns_none() -> None:
    assert (
        compute_next_run(
            "monthly_range",
            {"start_day": 15, "end_day": 15, "hour": 9, "minute": 0},
        )
        is None
    )


def test_monthly_range_missing_field_returns_none() -> None:
    assert compute_next_run("monthly_range", {"start_day": 15}) is None
    assert compute_next_run("monthly_range", {}) is None


# ══════════════════════════════════════════════════════════════
# MultiWeekday (extension F1.5 — jours non-continus mardi + jeudi)
# ══════════════════════════════════════════════════════════════


def test_multi_weekday_returns_next_listed_day() -> None:
    # [1,3] = mardi + jeudi, 09:00. Vendredi → prochain = mardi 28 avril.
    result = compute_next_run(
        "multi_weekday",
        {"weekdays": [1, 3], "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 28, 9, 0, 0, tzinfo=UTC)


def test_multi_weekday_today_listed_future_hour_returns_today() -> None:
    # [0,2,4] = lun/mer/ven, 14:00. Vendredi 12:00 → aujourd'hui 14:00.
    result = compute_next_run(
        "multi_weekday",
        {"weekdays": [0, 2, 4], "hour": 14, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 24, 14, 0, 0, tzinfo=UTC)


def test_multi_weekday_today_listed_past_hour_returns_next_listed_day() -> None:
    # [0,2,4], 09:00. Vendredi 09:00 passé → prochain = lundi 27 avril.
    result = compute_next_run(
        "multi_weekday",
        {"weekdays": [0, 2, 4], "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    assert result == datetime(2026, 4, 27, 9, 0, 0, tzinfo=UTC)


def test_multi_weekday_accepts_unsorted_input() -> None:
    # compute_next_run reconstruit un set → l'ordre d'entrée n'importe pas.
    result = compute_next_run(
        "multi_weekday",
        {"weekdays": [5, 1], "hour": 9, "minute": 0},
        from_dt=_FIXED_NOW,
    )
    # Vendredi → prochain dans {samedi, mardi} = samedi 25 avril.
    assert result == datetime(2026, 4, 25, 9, 0, 0, tzinfo=UTC)


def test_multi_weekday_single_day_returns_none() -> None:
    assert compute_next_run("multi_weekday", {"weekdays": [3], "hour": 9, "minute": 0}) is None


def test_multi_weekday_missing_weekdays_returns_none() -> None:
    assert compute_next_run("multi_weekday", {"hour": 9, "minute": 0}) is None


def test_multi_weekday_weekday_out_of_bounds_returns_none() -> None:
    assert compute_next_run("multi_weekday", {"weekdays": [1, 8], "hour": 9, "minute": 0}) is None


def test_multi_weekday_invalid_hour_returns_none() -> None:
    assert compute_next_run("multi_weekday", {"weekdays": [1, 3], "hour": 25, "minute": 0}) is None


# ══════════════════════════════════════════════════════════════
# yearly_range (F1.6 — mois figé + range de jours, récurrence annuelle)
# `_FIXED_NOW` = vendredi 24 avril 2026 12:00 UTC.
# ══════════════════════════════════════════════════════════════


def _yr(month: int, start: int, end: int, hour: int = 9, minute: int = 0) -> dict:
    return {
        "month": month,
        "start_day": start,
        "end_day": end,
        "hour": hour,
        "minute": minute,
    }


def test_yearly_range_future_this_year_returns_first_day() -> None:
    # Now = 24 avril → range « du 15 au 20 juin » → 15 juin 2026 09:00.
    result = compute_next_run("yearly_range", _yr(6, 15, 20), from_dt=_FIXED_NOW)
    assert result == datetime(2026, 6, 15, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_inside_range_hour_not_passed_returns_today() -> None:
    # On est le 17 juin 08:00, range 15→20 à 09:00 → aujourd'hui 09:00.
    now = datetime(2026, 6, 17, 8, 0, 0, tzinfo=UTC)
    result = compute_next_run("yearly_range", _yr(6, 15, 20), from_dt=now)
    assert result == datetime(2026, 6, 17, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_inside_range_hour_passed_returns_tomorrow() -> None:
    # On est le 17 juin 10:00, heure 09:00 passée → 18 juin (encore dans le range).
    now = datetime(2026, 6, 17, 10, 0, 0, tzinfo=UTC)
    result = compute_next_run("yearly_range", _yr(6, 15, 20), from_dt=now)
    assert result == datetime(2026, 6, 18, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_last_day_passed_returns_next_year() -> None:
    # On est le 20 juin 10:00, dernier jour du range, heure passée → 15 juin 2027.
    now = datetime(2026, 6, 20, 10, 0, 0, tzinfo=UTC)
    result = compute_next_run("yearly_range", _yr(6, 15, 20), from_dt=now)
    assert result == datetime(2027, 6, 15, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_month_already_passed_returns_next_year() -> None:
    # Now = avril 2026, range en janvier → janvier 2027 (le mois est figé).
    result = compute_next_run("yearly_range", _yr(1, 10, 15), from_dt=_FIXED_NOW)
    assert result == datetime(2027, 1, 10, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_february_leap_year_allows_29() -> None:
    # 2028 est bissextile : février a 29 jours, le range peut produire le 29.
    now = datetime(2028, 2, 29, 8, 0, 0, tzinfo=UTC)
    result = compute_next_run("yearly_range", _yr(2, 10, 29), from_dt=now)
    assert result == datetime(2028, 2, 29, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_february_non_leap_clamps_end_day() -> None:
    # 2026 non bissextile : février a 28 jours. Range « du 10 au 29 » →
    # end clampé à 28. Le 28 fév 09:30 a déjà passé l'heure 09:00, et le
    # 29 fév 2026 N'EXISTE PAS → sans le clamp, datetime() crasherait.
    # La tâche bascule sur février 2027 (aussi non bissextile).
    now = datetime(2026, 2, 28, 9, 30, 0, tzinfo=UTC)
    result = compute_next_run("yearly_range", _yr(2, 10, 29), from_dt=now)
    assert result == datetime(2027, 2, 10, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_february_non_leap_clamp_within_year() -> None:
    # Même range « du 10 au 29 février » mais on est le 20 fév 2026 08:00 :
    # le prochain run reste cette année, le 20 fév 09:00.
    now = datetime(2026, 2, 20, 8, 0, 0, tzinfo=UTC)
    result = compute_next_run("yearly_range", _yr(2, 10, 29), from_dt=now)
    assert result == datetime(2026, 2, 20, 9, 0, 0, tzinfo=UTC)


def test_yearly_range_missing_month_returns_none() -> None:
    assert (
        compute_next_run(
            "yearly_range",
            {"start_day": 15, "end_day": 20, "hour": 9, "minute": 0},
            from_dt=_FIXED_NOW,
        )
        is None
    )


def test_yearly_range_start_after_end_returns_none() -> None:
    assert compute_next_run("yearly_range", _yr(6, 20, 10), from_dt=_FIXED_NOW) is None


def test_yearly_range_single_day_returns_none() -> None:
    # start == end → invalide pour un range.
    assert compute_next_run("yearly_range", _yr(6, 15, 15), from_dt=_FIXED_NOW) is None


def test_yearly_range_invalid_hour_returns_none() -> None:
    assert compute_next_run("yearly_range", _yr(6, 15, 20, hour=25), from_dt=_FIXED_NOW) is None
