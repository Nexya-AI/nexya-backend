"""
Schémas Pydantic Planner — tâches planifiées F1.

Contrat API :
- `TaskCreate` : prompt + expert + schedule (discriminé) + options.
- `TaskUpdate` : partial update.
- `TaskResponse` from_attributes.
- `TaskResultResponse` from_attributes.

Discriminateur Pydantic v2 sur `schedule.type` pour que chaque config
soit validée selon son type (`once` requiert `at`, `interval_minutes`
requiert `minutes ≥ min_setting`, etc.).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    field_validator,
    model_validator,
)

# ══════════════════════════════════════════════════════════════
# Literals partagés
# ══════════════════════════════════════════════════════════════

ScheduleType = Literal[
    "once",
    "interval_minutes",
    "daily",
    "weekly",
    "monthly",
    "yearly",
    "weekly_range",
    "monthly_range",
    "multi_weekday",
    "yearly_range",
]
TaskStatus = Literal["idle", "pending", "running", "completed", "failed", "paused"]
ResultStatus = Literal["success", "failed", "skipped"]

# Borne maximale de jours par mois — 29 pour février : le 29/02 est
# accepté à la validation, le clamp en année non bissextile arrive au
# calcul `next_run_at`. Le 30/02, le 31/04, etc. sont rejetés car
# structurellement impossibles quelle que soit l'année. Partagé par
# `YearlyConfig` (F0.5) et `YearlyRangeConfig` (F1.6).
_MAX_DAYS_PER_MONTH: dict[int, int] = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


def _ensure_day_in_month(month: int, day: int, *, field: str) -> None:
    """Lève `ValueError` si `day` ne peut pas exister dans `month`.

    Le 29 février est accepté (clamp à 28 sur année non bissextile au
    calcul `next_run_at`). Le 30 février, le 31 avril, le 31 juin et le
    31 septembre/novembre sont rejetés — ils n'existent jamais.
    """
    if day > _MAX_DAYS_PER_MONTH[month]:
        raise ValueError(f"`{field}` ({day}) n'existe pas dans le mois {month}.")


# ══════════════════════════════════════════════════════════════
# Schedule configs discriminés
# ══════════════════════════════════════════════════════════════


class OnceConfig(BaseModel):
    """Exécution unique à une date/heure future UTC."""

    type: Literal["once"] = "once"
    at: datetime

    @field_validator("at", mode="after")
    @classmethod
    def _at_must_be_future(cls, v: datetime) -> datetime:
        # Normalise UTC aware.
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        else:
            v = v.astimezone(UTC)
        now = datetime.now(tz=UTC)
        if v <= now:
            raise ValueError("La date d'exécution `at` doit être dans le futur.")
        return v


class IntervalMinutesConfig(BaseModel):
    """Exécution répétée toutes les N minutes."""

    type: Literal["interval_minutes"] = "interval_minutes"
    minutes: int = Field(ge=1, le=1440)

    @field_validator("minutes", mode="after")
    @classmethod
    def _enforce_min_interval(cls, v: int) -> int:
        # Lecture tardive du setting pour éviter circulaire au module-load.
        from app.config import settings  # noqa: PLC0415

        if v < settings.tasks_min_interval_minutes:
            raise ValueError(
                f"L'intervalle minimum est "
                f"{settings.tasks_min_interval_minutes} minutes "
                f"(reçu {v})."
            )
        return v


class DailyConfig(BaseModel):
    """Exécution quotidienne à HH:MM UTC."""

    type: Literal["daily"] = "daily"
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class WeeklyConfig(BaseModel):
    """Exécution hebdomadaire à HH:MM UTC un jour ISO weekday (lundi=0)."""

    type: Literal["weekly"] = "weekly"
    weekday: int = Field(ge=0, le=6)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class MonthlyConfig(BaseModel):
    """Exécution mensuelle au jour `day` du mois à HH:MM UTC.

    Clamp implicite (côté `compute_next_run`) si le mois courant ne
    contient pas le jour demandé : `day=31` en février → 28 ou 29.
    """

    type: Literal["monthly"] = "monthly"
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class YearlyConfig(BaseModel):
    """Exécution annuelle au `month`/`day` à HH:MM UTC.

    Clamp implicite côté `compute_next_run` pour le 29 février sur
    année non bissextile → 28 février.
    """

    type: Literal["yearly"] = "yearly"
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @model_validator(mode="after")
    def _validate_month_day(self) -> YearlyConfig:
        _ensure_day_in_month(self.month, self.day, field="day")
        return self


class WeeklyRangeConfig(BaseModel):
    """Exécution sur un range continu de jours de semaine à HH:MM UTC (F1.5).

    Exemple : `start_weekday=0, end_weekday=4` (lundi → vendredi, jours
    ouvrés). Le range va du jour `start` au jour `end` inclus. Si
    `start_weekday > end_weekday`, le range enjambe le weekend
    (ex : `start=5, end=1` = samedi → mardi via dimanche + lundi).

    Convention weekday : 0 = lundi … 6 = dimanche (aligné sur
    `datetime.weekday()` — voir `_ISO_WEEKDAYS` dans `scheduler.py`).
    """

    type: Literal["weekly_range"] = "weekly_range"
    start_weekday: int = Field(ge=0, le=6)
    end_weekday: int = Field(ge=0, le=6)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @model_validator(mode="after")
    def _validate_range_not_single_day(self) -> WeeklyRangeConfig:
        # `start == end` = un seul jour → préférer `weekly` (atomique).
        if self.start_weekday == self.end_weekday:
            raise ValueError(
                "Pour un seul jour de la semaine, utilisez `weekly` "
                "au lieu de `weekly_range`."
            )
        return self


class MonthlyRangeConfig(BaseModel):
    """Exécution sur un range continu de jours du mois à HH:MM UTC (F1.5).

    Exemple : `start_day=15, end_day=30` (deuxième moitié du mois).
    Contrainte : `start_day < end_day` strict. Clamp implicite côté
    `compute_next_run` si le mois ne contient pas le jour demandé
    (ex : `end_day=31` en février → 28 ou 29 ; `start_day=31` en avril
    → 30).
    """

    type: Literal["monthly_range"] = "monthly_range"
    start_day: int = Field(ge=1, le=31)
    end_day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @model_validator(mode="after")
    def _validate_range_ordered(self) -> MonthlyRangeConfig:
        if self.start_day > self.end_day:
            raise ValueError(
                f"`start_day` ({self.start_day}) doit être strictement "
                f"inférieur à `end_day` ({self.end_day})."
            )
        if self.start_day == self.end_day:
            raise ValueError(
                "Pour un seul jour du mois, utilisez `monthly` "
                "au lieu de `monthly_range`."
            )
        return self


class MultiWeekdayConfig(BaseModel):
    """Exécution sur une liste de jours de semaine non-continus à HH:MM UTC (F1.5).

    Exemple : `weekdays=[1, 3]` (mardi + jeudi) — différent du range
    weekday qui imposerait la continuité.

    Validation : minimum 2 jours (sinon utiliser `weekly`), maximum 6
    jours (sinon utiliser `daily`). La liste est dédupliquée puis triée
    par le validator (idempotence DB + cohérence cache).
    Convention weekday : 0 = lundi … 6 = dimanche.
    """

    type: Literal["multi_weekday"] = "multi_weekday"
    weekdays: list[int] = Field(min_length=2, max_length=6)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @model_validator(mode="after")
    def _validate_weekdays(self) -> MultiWeekdayConfig:
        # Toutes les valeurs dans la borne 0-6.
        if not all(0 <= wd <= 6 for wd in self.weekdays):
            raise ValueError(
                "Chaque jour doit être compris entre 0 (lundi) et 6 (dimanche)."
            )
        # Doublons interdits (avant tri pour message d'erreur explicite).
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Doublons interdits dans `weekdays`.")
        # Normalisation : tri croissant (idempotence DB + dédup cache).
        self.weekdays = sorted(self.weekdays)
        return self


class YearlyRangeConfig(BaseModel):
    """Exécution sur un range de jours dans un mois précis, chaque année (F1.6).

    Exemple : `month=1, start_day=15, end_day=30` — chaque année, en
    janvier, du 15 au 30 inclus, à HH:MM UTC. La tâche s'exécute chaque
    jour du range.

    Différence clé avec `monthly_range` : ici le mois est **figé** (la
    récurrence est annuelle), alors que `monthly_range` se répète *tous*
    les mois.

    Contraintes :
    - `start_day < end_day` strict (un seul jour → utilisez `yearly`).
    - `start_day` et `end_day` doivent exister dans `month` : le 31 avril
      est rejeté ; le 29 février est accepté et clampé à 28 sur année non
      bissextile au calcul `next_run_at` (cohérent avec `yearly` F0.5).
    """

    type: Literal["yearly_range"] = "yearly_range"
    month: int = Field(ge=1, le=12)
    start_day: int = Field(ge=1, le=31)
    end_day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)

    @model_validator(mode="after")
    def _validate_range(self) -> YearlyRangeConfig:
        if self.start_day > self.end_day:
            raise ValueError(
                f"`start_day` ({self.start_day}) doit être strictement "
                f"inférieur à `end_day` ({self.end_day})."
            )
        if self.start_day == self.end_day:
            raise ValueError(
                "Pour un seul jour de l'année, utilisez `yearly` "
                "au lieu de `yearly_range`."
            )
        # Le mois est figé : les deux bornes doivent exister dans ce mois.
        _ensure_day_in_month(self.month, self.start_day, field="start_day")
        _ensure_day_in_month(self.month, self.end_day, field="end_day")
        return self


# Union discriminée par `type`.
ScheduleConfig = Annotated[
    OnceConfig
    | IntervalMinutesConfig
    | DailyConfig
    | WeeklyConfig
    | MonthlyConfig
    | YearlyConfig
    | WeeklyRangeConfig
    | MonthlyRangeConfig
    | MultiWeekdayConfig
    | YearlyRangeConfig,
    Discriminator("type"),
]


# ══════════════════════════════════════════════════════════════
# Requêtes
# ══════════════════════════════════════════════════════════════


class TaskCreate(BaseModel):
    """Body de `POST /tasks`."""

    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=4000)
    expert_id: str = Field(default="general", min_length=1, max_length=32)
    schedule: ScheduleConfig
    timezone: str = Field(default="UTC", max_length=64)
    auto_delete_after_run: bool = False

    @field_validator("title", "prompt", mode="before")
    @classmethod
    def _strip_strings(cls, v):
        return v.strip() if isinstance(v, str) else v


class TaskUpdate(BaseModel):
    """Body de `PATCH /tasks/{id}` — champs optionnels pour partial update."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    expert_id: str | None = Field(default=None, min_length=1, max_length=32)
    schedule: ScheduleConfig | None = None
    auto_delete_after_run: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> TaskUpdate:
        if all(
            v is None
            for v in (
                self.title,
                self.prompt,
                self.expert_id,
                self.schedule,
                self.auto_delete_after_run,
            )
        ):
            raise ValueError("Au moins un champ doit être fourni pour le PATCH.")
        return self


# ══════════════════════════════════════════════════════════════
# Réponses
# ══════════════════════════════════════════════════════════════


class TaskResponse(BaseModel):
    """Task sérialisée pour le client."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    prompt: str
    expert_id: str
    schedule_type: ScheduleType
    schedule_config: dict[str, Any]
    timezone: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    status: TaskStatus
    active: bool
    paused: bool
    auto_delete_after_run: bool
    retry_count: int
    max_retries: int
    run_count: int
    created_at: datetime
    updated_at: datetime


class TasksPage(BaseModel):
    """Page keyset de tâches."""

    items: list[TaskResponse]
    next_cursor: str | None = None


class TaskResultResponse(BaseModel):
    """Résultat d'une exécution sérialisé."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: uuid.UUID
    ran_at: datetime
    duration_ms: int
    status: ResultStatus
    result_text: str | None
    error_text: str | None
    tokens_input: int
    tokens_output: int
    cost_usd: Decimal
    model: str | None
    provider: str | None


class TaskResultsPage(BaseModel):
    items: list[TaskResultResponse]
    next_cursor: str | None = None
