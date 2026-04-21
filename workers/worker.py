"""
Worker arq — point d'entrée du process de tâches asynchrones.

Lancement :
    arq workers.worker.WorkerSettings

À l'avenir ce worker portera aussi les tâches du Prompt Scheduler
(Feature Planner) et tout job long qui n'a pas sa place dans le cycle
HTTP de FastAPI.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq.connections import RedisSettings
from arq.cron import cron

from app.config import settings
from app.core.database.postgres import dispose_engine
from app.core.observability import configure_logging
from workers.auth_tasks import cleanup_refresh_tokens
from workers.chat_tasks import generate_conversation_title

log = structlog.get_logger()


async def startup(ctx: dict[str, Any]) -> None:
    """Appelé une fois au démarrage du process worker."""
    configure_logging()
    log.info("worker.startup", env=settings.env)


async def shutdown(ctx: dict[str, Any]) -> None:
    """Appelé une fois à l'arrêt — ferme proprement le pool SQLAlchemy."""
    await dispose_engine()
    log.info("worker.shutdown.complete")


class WorkerSettings:
    """Configuration arq — découverte automatique par la commande `arq`."""

    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # Tâches appelables explicitement par `enqueue_job("<name>")`.
    # `generate_conversation_title` est déclenché par le router chat quand
    # le placeholder assistant est finalisé sur le 2ᵉ échange complet.
    functions = [cleanup_refresh_tokens, generate_conversation_title]

    # Crons — heure UTC. 03:17 évite le créneau 03:00 pile (tempête d'horaires
    # ronds sur toute l'infra) tout en restant en heure creuse.
    cron_jobs = [
        cron(
            cleanup_refresh_tokens,
            name="cleanup_refresh_tokens_daily",
            hour=3,
            minute=17,
            run_at_startup=False,
        ),
    ]

    on_startup = startup
    on_shutdown = shutdown

    # Garde-fous — éviter qu'un job buggé monopolise le worker
    job_timeout = 300         # 5 min max par tâche
    max_jobs = 10             # concurrence sur un process
    keep_result = 3600        # 1h de rétention des résultats dans Redis
