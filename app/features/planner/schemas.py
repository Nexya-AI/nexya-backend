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

ScheduleType = Literal["once", "interval_minutes", "daily", "weekly"]
TaskStatus = Literal["idle", "pending", "running", "completed", "failed", "paused"]
ResultStatus = Literal["success", "failed", "skipped"]


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


# Union discriminée par `type`.
ScheduleConfig = Annotated[
    OnceConfig | IntervalMinutesConfig | DailyConfig | WeeklyConfig,
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
