"""
Configuration structlog centralisée.

- En production : sortie JSON (une ligne par log, prête pour Loki/Datadog/ELK).
- En développement : sortie colorée et lisible.
- Tous les logs héritent automatiquement des contextvars (trace_id, user_id…)
  binders par le middleware TraceIdMiddleware.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    """Branche structlog + stdlib logging sur une seule sortie structurée.

    Appelé une fois au démarrage de l'app (lifespan) — jamais depuis un worker,
    qui aura sa propre configuration (workers/worker.py).
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # `add_logger_name` est écarté : il exige un logger stdlib, on utilise
    # PrintLoggerFactory. Le nom d'événement structlog (ex: "nexya.startup")
    # remplace efficacement un nom de logger.
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,   # injecte trace_id, user_id, ...
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.is_production:
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
        log_level = logging.INFO
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
        log_level = logging.DEBUG if settings.debug else logging.INFO

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy) vers le même renderer structlog.
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # uvicorn logge déjà via propagation ; on baisse son niveau d'accès en prod
    # pour éviter le double log (notre middleware fait déjà l'access log).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
