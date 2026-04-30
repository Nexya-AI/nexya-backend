"""
Modèles ORM Planner — `ScheduledTask` + `ScheduledTaskResult` (F1).

Aligné migration `014_scheduled_tasks.py`. Pas de relation inverse
`User.tasks` (anti-N+1 systématique).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class ScheduledTask(Base, UUIDMixin):
    """Tâche IA planifiée — un prompt + un schedule.

    Le dispatcher (`workers/scheduler_tasks.dispatch_due_tasks`) scan
    cette table chaque minute via `SELECT ... FOR UPDATE SKIP LOCKED`
    sur l'index partiel `ix_tasks_next_run_due`.
    """

    __tablename__ = "scheduled_tasks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    expert_id: Mapped[str] = mapped_column(
        String(32),
        server_default="general",
        default="general",
        nullable=False,
    )

    schedule_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schedule_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64),
        server_default="UTC",
        default="UTC",
        nullable=False,
    )

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(
        String(16),
        server_default="idle",
        default="idle",
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        Boolean, server_default="true", default=True, nullable=False
    )
    paused: Mapped[bool] = mapped_column(
        Boolean, server_default="false", default=False, nullable=False
    )
    auto_delete_after_run: Mapped[bool] = mapped_column(
        Boolean, server_default="false", default=False, nullable=False
    )

    retry_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, server_default="2", default=2, nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0, nullable=False)

    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "schedule_type IN ('once','interval_minutes','daily','weekly')",
            name="ck_tasks_schedule_type",
        ),
        CheckConstraint(
            "status IN ('idle','pending','running','completed','failed','paused')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "retry_count >= 0 AND max_retries >= 0",
            name="ck_tasks_retries_non_neg",
        ),
        CheckConstraint("run_count >= 0", name="ck_tasks_run_count_non_neg"),
        CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name="ck_tasks_title_length",
        ),
        Index(
            "ix_tasks_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_tasks_next_run_due",
            "next_run_at",
            postgresql_where=text(
                "deleted_at IS NULL "
                "AND active = true "
                "AND paused = false "
                "AND next_run_at IS NOT NULL "
                "AND status NOT IN ('running','completed')"
            ),
        ),
    )


class ScheduledTaskResult(Base):
    """Historique d'une exécution de tâche. Purgé après 30 jours via cron."""

    __tablename__ = "scheduled_task_results"

    id: Mapped[int] = mapped_column(
        BigInteger, autoincrement=True, primary_key=True, nullable=False
    )

    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    duration_ms: Mapped[int] = mapped_column(Integer, server_default="0", default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    result_text: Mapped[str | None] = mapped_column(Text)
    error_text: Mapped[str | None] = mapped_column(Text)

    tokens_input: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    tokens_output: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6),
        server_default="0",
        default=Decimal("0"),
        nullable=False,
    )

    model: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(16))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "status IN ('success','failed','skipped')",
            name="ck_task_results_status",
        ),
        CheckConstraint("duration_ms >= 0", name="ck_task_results_duration"),
        CheckConstraint("cost_usd >= 0", name="ck_task_results_cost_non_neg"),
        Index("ix_task_results_task", "task_id", "ran_at"),
        Index("ix_task_results_user_ran", "user_id", "ran_at"),
    )
