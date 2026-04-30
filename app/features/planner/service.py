"""
TaskSchedulerService — CRUD tâches planifiées (F1).

8 méthodes statiques :
- `create_task` (quota + validation + compute next_run_at + INSERT)
- `list_for_user` (keyset `(created_at, id) DESC` + filtre status)
- `get_task` (404 IDOR-safe)
- `update_task` (partial + recompute next_run_at si schedule change)
- `soft_delete_task`
- `pause_task` / `resume_task`
- `list_results` (keyset `(ran_at, id) DESC`)

Discipline :
- 404 toujours sur IDOR (jamais 403) — alignement NEXYA.
- Commit en fin de chaque mutation.
- Cursor opaque base64 `{iso}|{uuid}` pattern ConversationService.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import (
    ResourceNotFoundException,
    TaskScheduleInvalidException,
    TasksQuotaExceededException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.planner.models import ScheduledTask, ScheduledTaskResult
from app.features.planner.scheduler import compute_next_run
from app.features.planner.schemas import (
    ScheduleConfig,
    TaskCreate,
    TaskUpdate,
)

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Curseurs keyset opaques
# ══════════════════════════════════════════════════════════════


def _encode_cursor(ts: datetime, row_id: int | uuid.UUID) -> str:
    payload = f"{ts.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationException("Curseur invalide.") from exc
    if "|" not in decoded:
        raise ValidationException("Curseur malformé.")
    iso_part, id_part = decoded.split("|", 1)
    try:
        ts = datetime.fromisoformat(iso_part)
    except ValueError as exc:
        raise ValidationException("Curseur invalide.") from exc
    return ts, id_part


# ══════════════════════════════════════════════════════════════
# DTO internes
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TasksPageOrm:
    items: list[ScheduledTask]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class TaskResultsPageOrm:
    items: list[ScheduledTaskResult]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# Helpers plan
# ══════════════════════════════════════════════════════════════


def _tasks_quota(user: User) -> tuple[int, str]:
    if getattr(user, "is_pro", False):
        return settings.tasks_max_pro, "pro"
    return settings.tasks_max_free, "free"


def _schedule_to_dict(schedule: ScheduleConfig) -> tuple[str, dict]:
    """Convertit un `ScheduleConfig` Pydantic en `(type, config_dict)`.

    On sérialise `at` en ISO string pour le stockage JSONB.
    """
    data = schedule.model_dump(mode="json")
    schedule_type = data.pop("type")
    return schedule_type, data


# ══════════════════════════════════════════════════════════════
# TaskSchedulerService
# ══════════════════════════════════════════════════════════════


class TaskSchedulerService:
    """CRUD tâches planifiées."""

    # ── Owner check 404 IDOR-safe ──────────────────────────────
    @staticmethod
    async def _get_owned(task_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> ScheduledTask:
        result = await db.execute(
            select(ScheduledTask).where(
                ScheduledTask.id == task_id,
                ScheduledTask.user_id == user_id,
                ScheduledTask.deleted_at.is_(None),
            )
        )
        task = result.scalar_one_or_none()
        if task is None:
            raise ResourceNotFoundException("Tâche")
        return task

    # ── CREATE ──────────────────────────────────────────────────
    @staticmethod
    async def create_task(user: User, body: TaskCreate, db: AsyncSession) -> ScheduledTask:
        """Pipeline : quota → compute next_run_at → INSERT."""
        # 1. Quota pré-flight.
        max_tasks, plan_label = _tasks_quota(user)
        count_stmt = (
            select(func.count())
            .select_from(ScheduledTask)
            .where(
                ScheduledTask.user_id == user.id,
                ScheduledTask.deleted_at.is_(None),
            )
        )
        active_count = int((await db.execute(count_stmt)).scalar_one() or 0)
        if active_count >= max_tasks:
            raise TasksQuotaExceededException(
                current=active_count, maximum=max_tasks, plan=plan_label
            )

        # 2. Sérialisation schedule + compute next_run_at.
        schedule_type, schedule_config = _schedule_to_dict(body.schedule)
        next_run = compute_next_run(schedule_type, schedule_config)
        if next_run is None:
            raise TaskScheduleInvalidException(
                "La tâche ne peut pas être planifiée dans le passé ou le schedule est invalide."
            )

        # 3. INSERT.
        task = ScheduledTask(
            user_id=user.id,
            title=body.title,
            prompt=body.prompt,
            expert_id=body.expert_id,
            schedule_type=schedule_type,
            schedule_config=schedule_config,
            timezone=body.timezone or "UTC",
            next_run_at=next_run,
            auto_delete_after_run=body.auto_delete_after_run,
            status="idle",
            active=True,
            paused=False,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        log.info(
            "planner.task.created",
            user_id=str(user.id),
            task_id=str(task.id),
            schedule_type=schedule_type,
            next_run_at=next_run.isoformat() if next_run else None,
            plan=plan_label,
        )
        return task

    # ── LIST ────────────────────────────────────────────────────
    @staticmethod
    async def list_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int = 20,
        status: str | None = None,
    ) -> TasksPageOrm:
        effective_limit = max(1, min(int(limit or 20), 50))
        conditions: list = [
            ScheduledTask.user_id == user.id,
            ScheduledTask.deleted_at.is_(None),
        ]
        if status is not None:
            conditions.append(ScheduledTask.status == status)
        if cursor:
            cur_created_at, cur_id = _decode_cursor(cursor)
            try:
                cur_uuid = uuid.UUID(cur_id)
            except ValueError as exc:
                raise ValidationException("Curseur UUID invalide.") from exc
            conditions.append(
                tuple_(ScheduledTask.created_at, ScheduledTask.id)
                < tuple_(cur_created_at, cur_uuid)
            )

        stmt = (
            select(ScheduledTask)
            .where(*conditions)
            .order_by(ScheduledTask.created_at.desc(), ScheduledTask.id.desc())
            .limit(effective_limit + 1)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        has_more = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)
        return TasksPageOrm(items=items, next_cursor=next_cursor)

    # ── GET ─────────────────────────────────────────────────────
    @staticmethod
    async def get_task(task_id: uuid.UUID, user: User, db: AsyncSession) -> ScheduledTask:
        return await TaskSchedulerService._get_owned(task_id, user.id, db)

    # ── UPDATE ──────────────────────────────────────────────────
    @staticmethod
    async def update_task(
        task_id: uuid.UUID,
        user: User,
        body: TaskUpdate,
        db: AsyncSession,
    ) -> ScheduledTask:
        task = await TaskSchedulerService._get_owned(task_id, user.id, db)
        changes = body.model_dump(exclude_unset=True)

        if "title" in changes and changes["title"] is not None:
            task.title = changes["title"]
        if "prompt" in changes and changes["prompt"] is not None:
            task.prompt = changes["prompt"]
        if "expert_id" in changes and changes["expert_id"] is not None:
            task.expert_id = changes["expert_id"]
        if "auto_delete_after_run" in changes and changes["auto_delete_after_run"] is not None:
            task.auto_delete_after_run = changes["auto_delete_after_run"]
        # Schedule change → recompute next_run_at.
        if body.schedule is not None:
            schedule_type, schedule_config = _schedule_to_dict(body.schedule)
            next_run = compute_next_run(schedule_type, schedule_config)
            if next_run is None:
                raise TaskScheduleInvalidException("Schedule invalide ou dans le passé.")
            task.schedule_type = schedule_type
            task.schedule_config = schedule_config
            task.next_run_at = next_run
            # Reset retry si schedule change (nouvelle politique).
            task.retry_count = 0
            if task.status in {"failed"}:
                task.status = "idle"

        task.updated_at = func.now()
        await db.commit()
        await db.refresh(task)
        log.info(
            "planner.task.updated",
            task_id=str(task.id),
            user_id=str(user.id),
            fields=list(changes.keys()),
        )
        return task

    # ── SOFT DELETE ─────────────────────────────────────────────
    @staticmethod
    async def soft_delete_task(task_id: uuid.UUID, user: User, db: AsyncSession) -> None:
        task = await TaskSchedulerService._get_owned(task_id, user.id, db)
        task.deleted_at = func.now()
        task.active = False
        task.next_run_at = None
        task.updated_at = func.now()
        await db.commit()
        log.info(
            "planner.task.soft_deleted",
            task_id=str(task.id),
            user_id=str(user.id),
        )

    # ── PAUSE / RESUME ──────────────────────────────────────────
    @staticmethod
    async def pause_task(task_id: uuid.UUID, user: User, db: AsyncSession) -> ScheduledTask:
        task = await TaskSchedulerService._get_owned(task_id, user.id, db)
        task.paused = True
        task.status = "paused"
        task.next_run_at = None
        task.updated_at = func.now()
        await db.commit()
        await db.refresh(task)
        return task

    @staticmethod
    async def resume_task(task_id: uuid.UUID, user: User, db: AsyncSession) -> ScheduledTask:
        task = await TaskSchedulerService._get_owned(task_id, user.id, db)
        task.paused = False
        task.status = "idle"
        task.retry_count = 0
        # Recompute next_run_at depuis le schedule actuel.
        next_run = compute_next_run(task.schedule_type, task.schedule_config or {})
        task.next_run_at = next_run
        task.updated_at = func.now()
        await db.commit()
        await db.refresh(task)
        return task

    # ── LIST RESULTS ────────────────────────────────────────────
    @staticmethod
    async def list_results(
        task_id: uuid.UUID,
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> TaskResultsPageOrm:
        # Owner check avant d'exposer des résultats.
        await TaskSchedulerService._get_owned(task_id, user.id, db)

        effective_limit = max(1, min(int(limit or 20), 50))
        conditions: list = [ScheduledTaskResult.task_id == task_id]
        if cursor:
            cur_ran_at, cur_id_raw = _decode_cursor(cursor)
            try:
                cur_id = int(cur_id_raw)
            except ValueError as exc:
                raise ValidationException("Curseur ID invalide.") from exc
            conditions.append(
                tuple_(ScheduledTaskResult.ran_at, ScheduledTaskResult.id)
                < tuple_(cur_ran_at, cur_id)
            )

        stmt = (
            select(ScheduledTaskResult)
            .where(*conditions)
            .order_by(
                ScheduledTaskResult.ran_at.desc(),
                ScheduledTaskResult.id.desc(),
            )
            .limit(effective_limit + 1)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        has_more = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.ran_at, last.id)
        return TaskResultsPageOrm(items=items, next_cursor=next_cursor)
