"""
Helper `compute_next_run` — calcule `next_run_at` selon le schedule.

Fonction pure synchrone, UTC partout. Types supportés
(F1 + F0.5 + F1.5 + F1.6) :
`once` / `interval_minutes` / `daily` / `weekly` / `monthly` / `yearly` /
`weekly_range` / `monthly_range` / `multi_weekday` / `yearly_range`.

Expressions cron full (`0 9 * * MON`) = session future si besoin
produit (nécessite dep `croniter`). Timezone user-spécifique =
session future (nécessite colonne `users.timezone` + `zoneinfo`).
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, time, timedelta
from typing import Any, Final

_ISO_WEEKDAYS: Final[dict[int, str]] = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def compute_next_run(
    schedule_type: str,
    schedule_config: dict[str, Any],
    *,
    from_dt: datetime | None = None,
) -> datetime | None:
    """Retourne le prochain `next_run_at` UTC pour un schedule donné.

    - `once` : retourne `at` si > from_dt, sinon None (tâche ne se relance pas).
    - `interval_minutes` : from_dt + N minutes.
    - `daily` : prochain jour à HH:MM UTC (aujourd'hui si HH:MM > from_dt,
      sinon demain).
    - `weekly` : prochain ISO weekday à HH:MM UTC (jour courant si passage
      pas encore, sinon semaine prochaine).

    Retourne None si le schedule est terminal (once passé) ou invalide.
    """
    base = from_dt if from_dt is not None else _now_utc()
    # Normalise base en UTC aware.
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)

    if schedule_type == "once":
        raw = schedule_config.get("at")
        if raw is None:
            return None
        try:
            at_dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=UTC)
        else:
            at_dt = at_dt.astimezone(UTC)
        return at_dt if at_dt > base else None

    if schedule_type == "interval_minutes":
        minutes = int(schedule_config.get("minutes", 0))
        if minutes < 1:
            return None
        return base + timedelta(minutes=minutes)

    if schedule_type == "daily":
        hour = int(schedule_config.get("hour", 0))
        minute = int(schedule_config.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > base:
            return candidate
        return candidate + timedelta(days=1)

    if schedule_type == "weekly":
        weekday = int(schedule_config.get("weekday", -1))
        hour = int(schedule_config.get("hour", 0))
        minute = int(schedule_config.get("minute", 0))
        if not (0 <= weekday <= 6 and 0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        # Python datetime.weekday() : lundi=0, dimanche=6 (aligné ISO).
        days_ahead = (weekday - base.weekday()) % 7
        candidate = (base + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= base:
            candidate = candidate + timedelta(days=7)
        return candidate

    if schedule_type == "monthly":
        day = int(schedule_config.get("day", 0))
        hour = int(schedule_config.get("hour", 0))
        minute = int(schedule_config.get("minute", 0))
        if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        candidate = _make_monthly_candidate(base.year, base.month, day, hour, minute)
        if candidate is not None and candidate > base:
            return candidate
        # Sinon avance d'un mois.
        next_year, next_month = (
            (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
        )
        return _make_monthly_candidate(next_year, next_month, day, hour, minute)

    if schedule_type == "yearly":
        month = int(schedule_config.get("month", 0))
        day = int(schedule_config.get("day", 0))
        hour = int(schedule_config.get("hour", 0))
        minute = int(schedule_config.get("minute", 0))
        if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        candidate = _make_monthly_candidate(base.year, month, day, hour, minute)
        if candidate is not None and candidate > base:
            return candidate
        return _make_monthly_candidate(base.year + 1, month, day, hour, minute)

    if schedule_type == "weekly_range":
        return _make_weekly_range_candidate(schedule_config, base)

    if schedule_type == "monthly_range":
        return _make_monthly_range_candidate(schedule_config, base)

    if schedule_type == "multi_weekday":
        return _make_multi_weekday_candidate(schedule_config, base)

    if schedule_type == "yearly_range":
        return _make_yearly_range_candidate(schedule_config, base)

    return None


def _make_monthly_candidate(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
) -> datetime | None:
    """Construit un datetime UTC en clampant `day` au dernier jour du mois.

    Exemples : `day=31` en avril (30 jours) → 30 avril.
    `day=29` en février année non bissextile → 28 février.
    """
    last_day = calendar.monthrange(year, month)[1]
    safe_day = min(day, last_day)
    return datetime(year, month, safe_day, hour, minute, 0, 0, tzinfo=UTC)


# ══════════════════════════════════════════════════════════════
# Helpers F1.5 — range schedules
# ══════════════════════════════════════════════════════════════


def _next_in_weekday_set(
    allowed: set[int],
    hour: int,
    minute: int,
    from_dt: datetime,
) -> datetime | None:
    """Prochain datetime UTC strictement > `from_dt` dont le weekday ∈ `allowed`.

    Balaye jusqu'à 8 jours dans le futur — couvre le cas « aujourd'hui
    est un jour autorisé mais l'heure HH:MM est déjà passée » : on
    retombe alors sur le même weekday la semaine suivante.

    `allowed` utilise la convention `datetime.weekday()` (lundi = 0).
    """
    for delta in range(8):
        candidate_date = from_dt.date() + timedelta(days=delta)
        if candidate_date.weekday() not in allowed:
            continue
        candidate = datetime.combine(
            candidate_date,
            time(hour=hour, minute=minute),
            tzinfo=UTC,
        )
        if candidate > from_dt:
            return candidate
    return None


def _make_weekly_range_candidate(
    config: dict[str, Any],
    from_dt: datetime,
) -> datetime | None:
    """Prochaine exécution dans le range weekday `[start, end]` inclus.

    - `start <= end` : range continu simple (ex : lundi→vendredi = 0→4).
    - `start > end` : le range enjambe le weekend (ex : 5→1 = samedi,
      dimanche, lundi, mardi).

    Retourne `None` si la config est structurellement invalide (champ
    absent, valeur hors borne, `start == end`) — `compute_next_run` ne
    lève jamais.
    """
    start = int(config.get("start_weekday", -1))
    end = int(config.get("end_weekday", -1))
    hour = int(config.get("hour", -1))
    minute = int(config.get("minute", -1))
    if not (
        0 <= start <= 6
        and 0 <= end <= 6
        and start != end
        and 0 <= hour <= 23
        and 0 <= minute <= 59
    ):
        return None

    if start <= end:
        allowed = set(range(start, end + 1))
    else:
        # Enjambe le weekend : {start..6} ∪ {0..end}.
        allowed = set(range(start, 7)) | set(range(0, end + 1))

    return _next_in_weekday_set(allowed, hour, minute, from_dt)


def _make_multi_weekday_candidate(
    config: dict[str, Any],
    from_dt: datetime,
) -> datetime | None:
    """Prochaine exécution sur une liste explicite de jours de semaine.

    `weekdays` est déjà trié + dédupliqué par le validator Pydantic,
    mais on revalide défensivement (la config provient d'un JSONB DB
    et pourrait être malformée).

    Retourne `None` sur config invalide (liste absente, < 2 jours,
    valeur hors borne, HH:MM invalide).
    """
    raw = config.get("weekdays")
    hour = int(config.get("hour", -1))
    minute = int(config.get("minute", -1))
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return None
    try:
        allowed = {int(wd) for wd in raw}
    except (TypeError, ValueError):
        return None
    if not all(0 <= wd <= 6 for wd in allowed):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    return _next_in_weekday_set(allowed, hour, minute, from_dt)


def _make_monthly_range_candidate(
    config: dict[str, Any],
    from_dt: datetime,
) -> datetime | None:
    """Prochaine exécution dans le range jour-du-mois `[start, end]` inclus.

    `start` et `end` sont clampés au dernier jour réel du mois ciblé
    (ex : `start=30, end=31` en février → 28 ou 29 ; `end=31` en avril
    → 30). Si tout le range est déjà passé ce mois-ci, on bascule au
    premier jour du range le mois suivant.

    Retourne `None` si la config est invalide (`start >= end`, champ
    absent, hors borne, HH:MM invalide).
    """
    start = int(config.get("start_day", 0))
    end = int(config.get("end_day", 0))
    hour = int(config.get("hour", -1))
    minute = int(config.get("minute", -1))
    if not (
        1 <= start <= 31
        and 1 <= end <= 31
        and start < end
        and 0 <= hour <= 23
        and 0 <= minute <= 59
    ):
        return None

    last_day = calendar.monthrange(from_dt.year, from_dt.month)[1]
    eff_start = min(start, last_day)
    eff_end = min(end, last_day)

    if eff_start <= from_dt.day <= eff_end:
        # On est dans le range ce mois-ci.
        candidate = from_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > from_dt:
            return candidate
        # Heure passée aujourd'hui → jour suivant tant qu'on reste dans le range.
        if from_dt.day + 1 <= eff_end:
            return from_dt.replace(
                day=from_dt.day + 1,
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
        # Dernier jour du range passé → bascule mois suivant (fallthrough).
    elif from_dt.day < eff_start:
        # Avant le range ce mois-ci → premier jour du range.
        return from_dt.replace(
            day=eff_start, hour=hour, minute=minute, second=0, microsecond=0
        )

    # Range entièrement passé ce mois-ci → premier jour du range le mois suivant.
    next_month = from_dt.month % 12 + 1
    next_year = from_dt.year + (1 if next_month == 1 else 0)
    next_last_day = calendar.monthrange(next_year, next_month)[1]
    return datetime(
        next_year,
        next_month,
        min(start, next_last_day),
        hour,
        minute,
        tzinfo=UTC,
    )


# ══════════════════════════════════════════════════════════════
# Helper F1.6 — yearly_range (mois figé + range de jours)
# ══════════════════════════════════════════════════════════════


def _make_yearly_range_candidate(
    config: dict[str, Any],
    from_dt: datetime,
) -> datetime | None:
    """Prochaine exécution dans le range jour `[start, end]` d'un mois figé.

    Contrairement à `monthly_range` (qui se répète tous les mois), le
    mois est ici **fixe** : la récurrence est annuelle. On essaie
    l'année courante de `from_dt`, puis l'année suivante si tout le
    range de cette année est déjà passé.

    `start` et `end` sont clampés au dernier jour réel du mois pour
    l'année considérée (`calendar.monthrange()`) : un range « du 15 au
    29 février » se réduit à « du 15 au 28 » sur année non bissextile.

    Retourne `None` si la config est structurellement invalide (champ
    absent, hors borne, `start >= end`, HH:MM invalide) — `compute_next_run`
    ne lève jamais.
    """
    month = int(config.get("month", 0))
    start = int(config.get("start_day", 0))
    end = int(config.get("end_day", 0))
    hour = int(config.get("hour", -1))
    minute = int(config.get("minute", -1))
    if not (
        1 <= month <= 12
        and 1 <= start <= 31
        and 1 <= end <= 31
        and start < end
        and 0 <= hour <= 23
        and 0 <= minute <= 59
    ):
        return None

    # Année courante puis suivante : si le range de cette année est
    # entièrement passé, on bascule automatiquement sur l'année d'après.
    for year in (from_dt.year, from_dt.year + 1):
        last_day = calendar.monthrange(year, month)[1]
        eff_start = min(start, last_day)
        eff_end = min(end, last_day)
        for day in range(eff_start, eff_end + 1):
            candidate = datetime(year, month, day, hour, minute, 0, 0, tzinfo=UTC)
            if candidate > from_dt:
                return candidate
    return None
