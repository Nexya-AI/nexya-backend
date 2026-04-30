"""
Workers arq — Planner Scheduler (F1).

3 fonctions exposées à WorkerSettings :
- `dispatch_due_tasks(ctx)` — cron chaque minute. Scan les tâches dues
  via `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 50`, marque bulk status
  `pending`, enqueue un job `execute_scheduled_task(task_id)` par
  tâche. Fail-silent sur Redis down.
- `execute_scheduled_task(ctx, task_id)` — worker task. Lock le row,
  consomme budget chat, appelle le LLM via router, stocke le résultat,
  recompute `next_run_at` + `last_run_at` + `run_count`. Retry
  transient (ProviderUnavailableError) jusqu'à `max_retries=2`.
- `cleanup_old_task_results(ctx)` — cron quotidien 04:17 UTC. DELETE
  les résultats > `tasks_results_retention_days` (30 j par défaut).

Stratégie concurrence :
- `SELECT ... FOR UPDATE SKIP LOCKED` permet à plusieurs workers arq
  de scanner en parallèle sans race (chaque worker prend un batch
  disjoint).
- `UPDATE status='pending'` bulk immédiat avant `enqueue_job` = idempotence
  double-check (si le job est redispatché, il voit `status='pending'` et
  ne re-exécute pas).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from sqlalchemy import delete, select, text, update

from app.ai.budget_tracker import get_budget_tracker
from app.ai.providers import ChatCompletionRequest
from app.ai.providers import ChatMessage as AiChatMessage
from app.ai.providers.base import (
    ProviderError,
    ProviderUnavailableError,
)
from app.ai.runtime import get_ai_router
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.core.errors.exceptions import RateLimitExceededException
from app.features.auth.models import User
from app.features.notifications.service import NotificationDispatcher
from app.features.planner.models import (
    ScheduledTask,
    ScheduledTaskResult,
)
from app.features.planner.scheduler import compute_next_run

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

RETRY_DEFER_MINUTES: Final[int] = 5
EXECUTION_MAX_OUTPUT_TOKENS: Final[int] = 2048


# ══════════════════════════════════════════════════════════════
# Pool arq lazy
# ══════════════════════════════════════════════════════════════

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


async def enqueue_task_execution(task_id: UUID) -> None:
    """Enqueue `execute_scheduled_task(task_id)`. Fail-silent."""
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job("execute_scheduled_task", str(task_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "planner.enqueue_failed",
            task_id=str(task_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# CRON — dispatch_due_tasks
# ══════════════════════════════════════════════════════════════


async def dispatch_due_tasks(ctx: dict[str, Any]) -> dict[str, Any]:
    """Scan les tâches dues et enqueue une exécution par tâche.

    Pattern PostgreSQL canonique pour queue-on-DB :
    1. `SELECT id FROM scheduled_tasks WHERE next_run_at <= NOW() AND
       active AND NOT paused AND status NOT IN ('running','completed')
       FOR UPDATE SKIP LOCKED LIMIT 50` — récupère un batch atomique,
       plusieurs workers en parallèle ne se marchent pas dessus.
    2. `UPDATE status='pending'` bulk immédiat dans la même transaction.
    3. `enqueue_job` par tâche après commit — les jobs échouent
       silencieusement si Redis down (fail-silent pattern).
    """
    log.debug("planner.dispatch.tick_start")
    dispatched: list[UUID] = []

    async with AsyncSessionLocal() as db:
        # SELECT FOR UPDATE SKIP LOCKED — PostgreSQL-only, ignoré par SQLite.
        sql = text(
            """
            SELECT id FROM scheduled_tasks
            WHERE deleted_at IS NULL
              AND active = true
              AND paused = false
              AND next_run_at IS NOT NULL
              AND next_run_at <= NOW()
              AND status NOT IN ('running','completed')
            ORDER BY next_run_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :batch_size
            """
        ).bindparams(batch_size=settings.tasks_dispatch_batch_size)
        result = await db.execute(sql)
        rows = result.all()
        task_ids: list[UUID] = [row[0] for row in rows]

        if task_ids:
            await db.execute(
                update(ScheduledTask)
                .where(ScheduledTask.id.in_(task_ids))
                .values(
                    status="pending",
                    updated_at=datetime.now(tz=UTC),
                )
            )
            await db.commit()
            dispatched = task_ids

    for tid in dispatched:
        await enqueue_task_execution(tid)

    log.info(
        "planner.dispatch.completed",
        dispatched=len(dispatched),
        batch_size=settings.tasks_dispatch_batch_size,
    )
    return {"dispatched": len(dispatched)}


# ══════════════════════════════════════════════════════════════
# WORKER — execute_scheduled_task
# ══════════════════════════════════════════════════════════════


async def execute_scheduled_task(ctx: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Exécute une tâche : LLM → INSERT result → recompute next_run_at.

    Short-circuits : deleted, paused, already running, completed.

    Fail-safe :
    - Budget chat user épuisé → INSERT result `status='skipped'` +
      reprogramme la tâche sur `next_run_at` standard (pas de retry).
    - `ProviderUnavailableError` (réseau, 5xx) → retry_count++ et
      next_run_at = now + 5 min jusqu'à max_retries, ensuite status=failed.
    - `ProviderError` non-retryable → INSERT result failed + status=failed
      (pas de retry).
    """
    t0 = time.monotonic()
    task_uuid = UUID(task_id)
    log.info("planner.execute.start", task_id=task_id)

    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_uuid)
        if task is None:
            log.warning("planner.execute.task_missing", task_id=task_id)
            return {"skipped": True, "reason": "missing"}
        if task.deleted_at is not None:
            log.info("planner.execute.skip_deleted", task_id=task_id)
            return {"skipped": True, "reason": "deleted"}
        if task.paused:
            log.info("planner.execute.skip_paused", task_id=task_id)
            return {"skipped": True, "reason": "paused"}
        if task.status == "running":
            log.info("planner.execute.skip_already_running", task_id=task_id)
            return {"skipped": True, "reason": "already_running"}
        if task.status == "completed":
            log.info("planner.execute.skip_completed", task_id=task_id)
            return {"skipped": True, "reason": "completed"}

        # Charge l'user owner.
        user_result = await db.execute(select(User).where(User.id == task.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            log.warning(
                "planner.execute.user_missing",
                task_id=task_id,
                user_id=str(task.user_id),
            )
            return {"skipped": True, "reason": "user_missing"}

        # Snapshot état pour recompute post-exécution.
        task.status = "running"
        task.updated_at = datetime.now(tz=UTC)
        await db.commit()
        await db.refresh(task)

    # Exécution hors de la session initiale (on ne tient pas une
    # transaction pendant l'appel LLM qui peut prendre plusieurs secondes).
    result_status = "success"
    result_text: str | None = None
    error_text: str | None = None
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    model = None
    provider_name = None
    should_retry_transient = False

    try:
        # Budget chat pré-flight — si épuisé → skipped (pas de retry).
        tracker = get_budget_tracker()
        try:
            await tracker.check_and_consume_chat(str(task.user_id), cost=1)
        except RateLimitExceededException:
            result_status = "skipped"
            error_text = "Quota chat journalier épuisé."
            log.info(
                "planner.execute.budget_exhausted",
                task_id=task_id,
                user_id=str(task.user_id),
            )
        else:
            # Appel LLM via router.
            try:
                resolution = get_ai_router().resolve(task.expert_id)
                req = ChatCompletionRequest(
                    messages=[
                        AiChatMessage(role="user", content=task.prompt),
                    ],
                    model=resolution.model,
                    temperature=0.2,
                    max_tokens=EXECUTION_MAX_OUTPUT_TOKENS,
                )
                parts: list[str] = []
                tokens_in_local = 0
                tokens_out_local = 0
                async for chunk in resolution.provider.stream_chat(req):
                    if chunk.delta:
                        parts.append(chunk.delta)
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        tokens_in_local = max(
                            tokens_in_local,
                            int(getattr(usage, "prompt_tokens", 0) or 0),
                        )
                        tokens_out_local = max(
                            tokens_out_local,
                            int(getattr(usage, "completion_tokens", 0) or 0),
                        )
                result_text = "".join(parts)
                tokens_in = tokens_in_local
                tokens_out = tokens_out_local
                model = resolution.model
                provider_name = resolution.provider.name
                log.info(
                    "planner.execute.llm_ok",
                    task_id=task_id,
                    provider=provider_name,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            except ProviderUnavailableError as exc:
                result_status = "failed"
                error_text = f"Provider indisponible : {exc}"
                should_retry_transient = True
                provider_name = getattr(exc, "provider", None)
                log.warning(
                    "planner.execute.provider_unavailable",
                    task_id=task_id,
                    error=str(exc),
                )
            except ProviderError as exc:
                result_status = "failed"
                error_text = f"Erreur provider : {exc}"
                provider_name = getattr(exc, "provider", None)
                log.warning(
                    "planner.execute.provider_error",
                    task_id=task_id,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                result_status = "failed"
                error_text = f"Erreur inattendue : {exc}"
                log.error(
                    "planner.execute.unexpected_error",
                    task_id=task_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
    except Exception as exc:  # noqa: BLE001
        # Dernier garde-fou (Budget tracker crash, etc.) — on ne plante pas
        # le worker, on remonte le flag et on reprogramme plus tard.
        result_status = "failed"
        error_text = f"Budget tracker ou setup KO : {exc}"
        should_retry_transient = True

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Recompute next_run_at + persistance dans une nouvelle session.
    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_uuid)
        if task is None:
            log.warning(
                "planner.execute.task_vanished_post_exec",
                task_id=task_id,
            )
            return {"skipped": True, "reason": "vanished"}

        # INSERT result
        result_row = ScheduledTaskResult(
            task_id=task_uuid,
            user_id=task.user_id,
            duration_ms=duration_ms,
            status=result_status,
            result_text=result_text if result_status == "success" else None,
            error_text=error_text,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=cost_usd,
            model=model,
            provider=provider_name,
        )
        db.add(result_row)

        # Recompute status + next_run_at selon le résultat.
        now_utc = datetime.now(tz=UTC)
        task.last_run_at = now_utc
        task.run_count = (task.run_count or 0) + 1

        if should_retry_transient and task.retry_count < task.max_retries:
            # Retry transient : re-scheduler dans 5 min.
            task.retry_count += 1
            task.next_run_at = now_utc + timedelta(minutes=RETRY_DEFER_MINUTES)
            task.status = "idle"
            log.info(
                "planner.execute.retry_scheduled",
                task_id=task_id,
                retry_count=task.retry_count,
                next_run=task.next_run_at.isoformat(),
            )
        elif result_status == "failed":
            # Non-retryable ou max_retries atteint.
            task.status = "failed"
            task.next_run_at = None
            log.info(
                "planner.execute.final_failure",
                task_id=task_id,
                retry_count=task.retry_count,
            )
        else:
            # Success ou skipped : reset retry + recompute next_run_at.
            task.retry_count = 0
            next_run = compute_next_run(task.schedule_type, task.schedule_config or {})
            if next_run is None:
                # Tâche 'once' terminée.
                task.status = "completed"
                task.next_run_at = None
                if task.auto_delete_after_run and result_status == "success":
                    task.deleted_at = now_utc
                    task.active = False
                    log.info(
                        "planner.execute.auto_deleted_after_run",
                        task_id=task_id,
                    )
            else:
                task.next_run_at = next_run
                task.status = "idle"

        task.updated_at = now_utc
        await db.commit()

    log.info(
        "planner.execute.completed",
        task_id=task_id,
        result_status=result_status,
        duration_ms=duration_ms,
    )

    # ── F3 : dispatcher dual-channel (push + email fallback) fail-safe ──
    # Le dispatcher lit les préférences user, tente push, bascule sur email
    # en fallback si push KO ou pas de device actif (selon settings), et
    # trace une row `notifications` pour la timeline in-app. Fail-safe strict :
    # ne raise JAMAIS au caller — le worker arq ne doit pas crasher sur une
    # panne FCM/Brevo/DB.
    try:
        await _dispatch_task_notification(
            task_uuid=task_uuid,
            result_status=result_status,
            result_text=result_text,
        )
    except Exception as exc:  # noqa: BLE001 — ceinture + bretelles
        log.warning(
            "planner.notification.unexpected_error",
            task_id=task_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    return {
        "skipped": False,
        "status": result_status,
        "duration_ms": duration_ms,
    }


# ══════════════════════════════════════════════════════════════
# F3 — Hook NotificationDispatcher post-exécution
# ══════════════════════════════════════════════════════════════


def _build_task_notification_body(result_status: str, result_text: str | None) -> str:
    """Construit le body de notification (preview 140 chars max)."""
    preview_max = int(settings.fcm_body_preview_max_chars)
    if result_status == "success":
        body = (result_text or "").strip()
        if not body:
            return "Tâche exécutée."
        if len(body) > preview_max:
            body = body[: max(0, preview_max - 1)].rstrip() + "…"
        return body
    if result_status == "skipped":
        return "Tâche reportée (quota journalier atteint)."
    return "Échec — réessai automatique bientôt."


async def _dispatch_task_notification(
    *,
    task_uuid: UUID,
    result_status: str,
    result_text: str | None,
) -> None:
    """Délègue au `NotificationDispatcher` avec `category='tasks'`.

    Le dispatcher s'occupe de tout le reste : lookup préférences user,
    push FCM avec soft-delete UNREGISTERED auto, fallback email si
    préférence le permet, INSERT row `notifications` pour timeline
    in-app, log forensic complet.

    Ce helper reste minimal : charge task + user, construit le payload
    sémantique, délègue, c'est tout.
    """
    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_uuid)
        if task is None or task.deleted_at is not None:
            log.info(
                "planner.notification.skipped_task_gone",
                task_id=str(task_uuid),
            )
            return
        user = await db.get(User, task.user_id)
        if user is None:
            log.warning(
                "planner.notification.user_missing",
                task_id=str(task_uuid),
                user_id=str(task.user_id),
            )
            return

        title = f"NEXYA — {task.title}"
        body = _build_task_notification_body(result_status, result_text)
        task_id_str = str(task_uuid)
        data_payload: dict[str, Any] = {
            "task_id": task_id_str,
            "status": result_status,
            "deep_link": f"nexya://task/{task_id_str}",
            "task_title": task.title,
            "notification_kind": "completed",
        }

        await NotificationDispatcher.dispatch(
            user=user,
            category="tasks",
            title=title,
            body=body,
            data=data_payload,
            source_task_id=task_uuid,
            source_kind="scheduled_task",
            db=db,
        )


# ══════════════════════════════════════════════════════════════
# CRON — cleanup_old_task_results
# ══════════════════════════════════════════════════════════════


async def cleanup_old_task_results(
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Purge les résultats > `tasks_results_retention_days`."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=settings.tasks_results_retention_days)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(ScheduledTaskResult)
            .where(ScheduledTaskResult.ran_at < cutoff)
            .returning(ScheduledTaskResult.id)
        )
        deleted_ids = list(result.scalars().all())
        await db.commit()

    log.info(
        "planner.cleanup.completed",
        deleted_count=len(deleted_ids),
        retention_days=settings.tasks_results_retention_days,
    )
    return {"deleted": len(deleted_ids)}
