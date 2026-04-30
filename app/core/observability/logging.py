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
from typing import Any

import structlog

from app.config import settings


def _inject_otel_context(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Processor structlog — injecte trace_id + span_id du span OTel actif.

    Si OTel n'est pas installé OU pas d'init : no-op total (rétrocompat —
    le `trace_id` posé par `TraceIdMiddleware` reste celui du middleware).

    Si un span OTel est actif et valide : remplace `event_dict["trace_id"]`
    par le trace_id 32-hex de OTel et ajoute `span_id` 16-hex. C'est cette
    valeur que Tempo/Jaeger/Loki utiliseront pour corréler logs ↔ traces
    dans Grafana.

    Format hex strict — pas de tiret, lowercase. Important : Tempo et
    Jaeger UI parsent le trace_id en hex pur, un format différent casse
    le clic-pour-zoomer-sur-la-trace.

    Fail-safe absolu : toute exception levée est silencieusement absorbée,
    le log continue d'être émis même si OTel est cassé.
    """
    if not getattr(settings, "observability_log_trace_injection", True):
        return event_dict

    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is None:
            return event_dict
        ctx = span.get_span_context()
        if ctx is None or not getattr(ctx, "is_valid", False):
            return event_dict
        # Format 32 hex pour trace_id (128 bits) + 16 hex pour span_id
        # (64 bits) — c'est ce que Tempo/Jaeger UI attendent.
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    except Exception:  # noqa: BLE001 — fail-safe absolu
        return event_dict
    return event_dict


def configure_logging() -> None:
    """Branche structlog + stdlib logging sur une seule sortie structurée.

    Appelé une fois au démarrage de l'app (lifespan) — jamais depuis un worker,
    qui aura sa propre configuration (workers/worker.py).
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # `add_logger_name` est écarté : il exige un logger stdlib, on utilise
    # PrintLoggerFactory. Le nom d'événement structlog (ex: "nexya.startup")
    # remplace efficacement un nom de logger.
    # Ordre critique : `merge_contextvars` AVANT `_inject_otel_context`
    # pour que l'OTel injection puisse écraser le trace_id legacy si un
    # span OTel est actif.
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,  # injecte trace_id, user_id, ...
        _inject_otel_context,  # K1 — trace_id OTel + span_id
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
