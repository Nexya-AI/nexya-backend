"""
Helper `compute_next_run` — calcule `next_run_at` selon le schedule.

Fonction pure synchrone, UTC partout. Scope F1 : `once` /
`interval_minutes` / `daily` / `weekly`.

Expressions cron full (`0 9 * * MON`) = session future si besoin
produit (nécessite dep `croniter`). Timezone user-spécifique =
session future (nécessite colonne `users.timezone` + `zoneinfo`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

    return None
