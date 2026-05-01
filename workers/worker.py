"""
Worker arq — point d'entrée du process de tâches asynchrones.

Lancement :
    arq workers.worker.WorkerSettings

À l'avenir ce worker portera aussi les tâches du Prompt Scheduler
(Feature Planner) et tout job long qui n'a pas sa place dans le cycle
HTTP de FastAPI.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron

from app.config import settings
from app.core.database.postgres import dispose_engine
from app.core.observability import (
    configure_logging,
    get_tracer,
    record_arq_job,
    setup_otel,
    setup_prometheus,
    setup_sentry,
)
from workers.ai_tasks import flush_ai_sessions
from workers.auth_tasks import cleanup_refresh_tokens
from workers.chat_tasks import generate_conversation_title
from workers.chunk_tasks import index_document_chunks
from workers.memory_tasks import extract_durable_facts
from workers.rgpd_tasks import purge_deleted_accounts
from workers.scheduler_tasks import (
    cleanup_old_task_results,
    dispatch_due_tasks,
    execute_scheduled_task,
)

log = structlog.get_logger()


async def startup(ctx: dict[str, Any]) -> None:
    """Appelé une fois au démarrage du process worker."""
    configure_logging()
    # K1 — observabilité côté worker, ordre identique à l'API.
    # Le worker partage les mêmes settings → les flags OTEL_ENABLED /
    # SENTRY_DSN / PROMETHEUS_ENABLED gouvernent les deux process.
    # Note : pas d'instrumentation FastAPI ici (pas de FastAPI), pas
    # de db_engine au boot non plus (chaque job ouvre sa propre
    # AsyncSession). Les spans HTTP sortants (httpx LLM) restent
    # auto-instrumentés.
    setup_sentry(settings)
    setup_otel(settings)
    setup_prometheus(settings)
    log.info("worker.startup", env=settings.env)


async def shutdown(ctx: dict[str, Any]) -> None:
    """Appelé une fois à l'arrêt — ferme proprement le pool SQLAlchemy."""
    await dispose_engine()
    log.info("worker.shutdown.complete")


async def _on_job_start(ctx: dict[str, Any]) -> None:
    """K1 — Hook before_job arq : ouvre un span OTel + start timer.

    arq propage `ctx` à travers tout le cycle de vie d'un job ; on
    y stocke `_otel_span_cm`, `_otel_span` et `_started_at` que le
    after_job lira pour fermer proprement.
    """
    function_name = ctx.get("job_try", "?")
    job_id = ctx.get("job_id", "")
    function = ctx.get("enqueue_time", "")
    # arq met le nom de la fonction dans `ctx["job_try"]` ? Non —
    # c'est dans `ctx["enqueue_time"]`. On utilise `ctx["job_id"]`
    # et un attribut générique. Le nom réel n'est pas toujours dispo
    # côté before_job ; on lit `ctx["function_name"]` si présent (arq
    # 0.26+) sinon "unknown".
    fn_name = ctx.get("function_name") or "unknown"
    ctx["_started_at"] = time.monotonic()
    ctx["_fn_name"] = fn_name
    tracer = get_tracer()
    span_cm = tracer.start_as_current_span(
        f"arq.{fn_name}",
        attributes={
            "arq.function": fn_name,
            "arq.job_id": str(job_id) if job_id else "",
            "arq.try_count": ctx.get("job_try", 0),
        },
    )
    try:
        ctx["_otel_span"] = span_cm.__enter__()
        ctx["_otel_span_cm"] = span_cm
    except Exception:  # noqa: BLE001
        ctx["_otel_span"] = None
        ctx["_otel_span_cm"] = None


async def _on_job_end(ctx: dict[str, Any]) -> None:
    """K1 — Hook after_job arq : ferme le span + enregistre métriques.

    arq invoque `on_job_end` même si le job a levé (succes / fail /
    cancelled). On lit `ctx["job_status"]` si dispo pour décider
    l'outcome.
    """
    started = ctx.get("_started_at")
    duration = max(0.0, time.monotonic() - started) if started else 0.0
    fn_name = ctx.get("_fn_name") or "unknown"
    # arq 0.26 expose `ctx["job_status"]` mais sa présence dépend du
    # mode d'exécution. On déduit l'outcome depuis l'absence/présence
    # d'une exception captée — par défaut "completed".
    outcome = "completed"
    span = ctx.get("_otel_span")
    span_cm = ctx.get("_otel_span_cm")
    if span is not None:
        try:
            span.set_attribute("arq.outcome", outcome)
        except Exception:  # noqa: BLE001
            pass
    if span_cm is not None:
        try:
            span_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
    try:
        record_arq_job(fn_name, outcome, duration)
    except Exception:  # noqa: BLE001
        pass


class WorkerSettings:
    """Configuration arq — découverte automatique par la commande `arq`."""

    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # Tâches appelables explicitement par `enqueue_job("<name>")`.
    # `generate_conversation_title` est déclenché par le router chat quand
    # le placeholder assistant est finalisé sur le 2ᵉ échange complet.
    # `flush_ai_sessions` est aussi exposé hors cron pour pouvoir le
    # déclencher manuellement depuis un script d'ops (incident recovery).
    functions = [
        cleanup_refresh_tokens,
        generate_conversation_title,
        flush_ai_sessions,
        # D2 — extraction auto de faits durables après chaque conv complétée.
        # Enqueue depuis `_finalize_in_fresh_session` (router chat) quand
        # `message_count >= 6` ET `memory_extracted_at IS NULL`.
        extract_durable_facts,
        # D4 — indexation RAG des chunks de documents (PDF/DOCX/TXT/MD).
        # Enqueue depuis `FileUploadService.upload` après succès pipeline.
        # Idempotent via sentinelle `uploaded_files.chunks_indexed_at`.
        index_document_chunks,
        # F1 — exécution d'une tâche planifiée (Planner). Enqueue par
        # `dispatch_due_tasks` (cron chaque minute) qui scan les tâches
        # dues via `SELECT FOR UPDATE SKIP LOCKED`.
        execute_scheduled_task,
        # F1 — dispatch & cleanup. Listés aussi dans `functions` pour
        # pouvoir les déclencher manuellement via `enqueue_job` depuis
        # un script d'ops (incident recovery). Le scheduling réel passe
        # par `cron_jobs` ci-dessous.
        dispatch_due_tasks,
        cleanup_old_task_results,
        # J1 — purge différée RGPD (Article 17). Cron quotidien 03:17 UTC.
        # Listé dans `functions` pour permettre un déclenchement manuel
        # via `enqueue_job` depuis un script d'ops (incident recovery).
        purge_deleted_accounts,
    ]

    # Crons — heure UTC. 03:17 évite le créneau 03:00 pile (tempête d'horaires
    # ronds sur toute l'infra) tout en restant en heure creuse.
    # `flush_ai_sessions` tourne toutes les 10 minutes (minute=*/10) pour
    # rattraper les ai_calls qu'un crash uvicorn ou une panne DB auraient
    # fait tomber entre le fast path CostTracker et l'INSERT.
    cron_jobs = [
        cron(
            cleanup_refresh_tokens,
            name="cleanup_refresh_tokens_daily",
            hour=3,
            minute=17,
            run_at_startup=False,
        ),
        cron(
            flush_ai_sessions,
            name="flush_ai_sessions_every_10m",
            minute={0, 10, 20, 30, 40, 50},
            run_at_startup=False,
        ),
        # F1 — dispatcher Planner chaque minute. Scan les tâches dues
        # (next_run_at <= NOW()), marque pending, enqueue exécutions.
        cron(
            dispatch_due_tasks,
            name="dispatch_due_tasks_every_1m",
            minute={
                0,
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
                31,
                32,
                33,
                34,
                35,
                36,
                37,
                38,
                39,
                40,
                41,
                42,
                43,
                44,
                45,
                46,
                47,
                48,
                49,
                50,
                51,
                52,
                53,
                54,
                55,
                56,
                57,
                58,
                59,
            },
            run_at_startup=False,
        ),
        # F1 — purge des résultats > retention_days (30 j par défaut).
        # 04:17 UTC, aligné sur `cleanup_refresh_tokens` (créneau creux).
        cron(
            cleanup_old_task_results,
            name="cleanup_old_task_results_daily",
            hour=4,
            minute=23,
            run_at_startup=False,
        ),
        # J1 — purge différée RGPD (Article 17). Cron quotidien 03:47 UTC
        # (créneau creux entre cleanup_refresh_tokens 03:17 et
        # cleanup_old_task_results 04:23). SELECT FOR UPDATE SKIP LOCKED
        # batch=50 + DELETE FROM users cascade SQL + suppression blobs MinIO.
        cron(
            purge_deleted_accounts,
            name="purge_deleted_accounts_daily",
            hour=3,
            minute=47,
            run_at_startup=False,
        ),
    ]

    on_startup = startup
    on_shutdown = shutdown
    # K1 — instrumentation arq : 1 span + 1 métrique par job exécuté.
    on_job_start = _on_job_start
    on_job_end = _on_job_end

    # Garde-fous — éviter qu'un job buggé monopolise le worker
    job_timeout = 300  # 5 min max par tâche
    max_jobs = 10  # concurrence sur un process
    keep_result = 3600  # 1h de rétention des résultats dans Redis
