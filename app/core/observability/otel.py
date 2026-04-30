"""
OpenTelemetry — tracing distribué pour NEXYA.

Pattern :
- Auto-instrumentation des 5 couches transverses (FastAPI, SQLAlchemy,
  httpx, Redis, asgi) — les spans HTTP, DB et appels sortants
  apparaissent automatiquement, sans toucher au code des features.
- Spans manuels sur les chemins critiques NEXYA via `get_tracer()` :
  StreamHandler.stream, run_with_tool_rounds, NotificationDispatcher.
  Ces spans ajoutent du sens métier (provider/model/expert_id) que
  l'auto-instrumentation HTTP brute ne donne pas.
- Export OTLP/HTTP via `BatchSpanProcessor` vers
  `OTEL_EXPORTER_OTLP_ENDPOINT`. Si l'endpoint est inaccessible, le
  SDK envoie en silence vers le rien (fail-open) — un seul warning
  au boot, pas de crash service.
- Sampler `ParentBased(TraceIdRatioBased(ratio))` — honore la
  décision parent si un upstream a déjà sampled (cohérence trace
  cross-service), sinon tire à l'aléatoire selon le ratio.

Fail-safe absolu : toute exception levée pendant l'init est attrapée
et loguée. Le service NEXYA ne crashe JAMAIS à cause d'OTel.

Limitation OTel SDK 1.27 : `SQLAlchemyInstrumentor` ne supporte pas
directement un `AsyncEngine` — on lui passe `engine.sync_engine`
(le AsyncEngine wrappe un sync_engine en interne). Côté trace,
ça donne le même résultat que sur une app sync : 1 span par
requête SQL exécutée.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.config import Settings, settings

log = structlog.get_logger(__name__)


_INIT_ATTEMPTED = False
_INIT_OK = False
_TRACER: Any = None  # opentelemetry.trace.Tracer ou None
_PROVIDER: Any = None  # TracerProvider ou None


def setup_otel(
    cfg: Settings | None = None,
    *,
    app: Any = None,
    db_engine: Any = None,
) -> bool:
    """Initialise OTel + auto-instrumentation. Idempotent + fail-safe.

    Args:
        cfg: Settings NEXYA (par défaut le singleton `settings`).
        app: instance FastAPI à instrumenter (auto-spans HTTP par
            endpoint).
        db_engine: AsyncEngine SQLAlchemy à instrumenter. On lui
            passe `db_engine.sync_engine` au SDK OTel — limitation
            connue.

    Retour : True si actif après l'appel, False sinon (kill-switch
    off, exception d'init, SDK non installé).
    """
    global _INIT_ATTEMPTED, _INIT_OK, _TRACER, _PROVIDER

    cfg = cfg or settings
    if _INIT_ATTEMPTED:
        return _INIT_OK
    _INIT_ATTEMPTED = True

    if not cfg.otel_enabled:
        log.info("otel.disabled", reason="kill_switch_off")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )
    except ImportError as exc:
        log.warning("otel.import_failed", error=str(exc))
        return False

    try:
        resource = Resource.create(
            {
                "service.name": cfg.otel_service_name,
                "service.version": cfg.app_version,
                "deployment.environment": cfg.sentry_environment,
            }
        )
        sampler = ParentBased(root=TraceIdRatioBased(cfg.otel_traces_sampler_ratio))
        provider = TracerProvider(resource=resource, sampler=sampler)

        exporter = OTLPSpanExporter(
            endpoint=cfg.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces",
            timeout=5,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _PROVIDER = provider
        _TRACER = trace.get_tracer("nexya.backend", cfg.app_version)

        # Auto-instrumentations — chaque integration est isolée dans
        # son propre try/except : si l'une plante (version SDK
        # incompatible), les autres restent fonctionnelles.
        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import (
                    FastAPIInstrumentor,
                )

                FastAPIInstrumentor.instrument_app(app)
            except Exception as exc:  # noqa: BLE001
                log.warning("otel.fastapi_instr_failed", error=str(exc))

        if db_engine is not None:
            try:
                from opentelemetry.instrumentation.sqlalchemy import (
                    SQLAlchemyInstrumentor,
                )

                # Limitation OTel SDK 1.27 : passer le sync_engine
                # interne, pas l'AsyncEngine directement.
                target_engine = getattr(db_engine, "sync_engine", db_engine)
                SQLAlchemyInstrumentor().instrument(engine=target_engine)
            except Exception as exc:  # noqa: BLE001
                log.warning("otel.sqlalchemy_instr_failed", error=str(exc))

        try:
            from opentelemetry.instrumentation.httpx import (
                HTTPXClientInstrumentor,
            )

            HTTPXClientInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            log.warning("otel.httpx_instr_failed", error=str(exc))

        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
        except Exception as exc:  # noqa: BLE001
            log.warning("otel.redis_instr_failed", error=str(exc))

        _INIT_OK = True
        log.info(
            "otel.initialized",
            service_name=cfg.otel_service_name,
            endpoint=cfg.otel_exporter_otlp_endpoint,
            sampler_ratio=cfg.otel_traces_sampler_ratio,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "otel.init_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


def get_tracer() -> Any:
    """Retourne le tracer NEXYA (ou un no-op si OTel n'est pas init).

    Le no-op est sûr : `start_as_current_span` retourne un context
    manager qui ne fait rien, le code instrumenté tourne sans
    aucune dépendance dure à OTel.
    """
    if _TRACER is not None:
        return _TRACER
    try:
        from opentelemetry import trace

        return trace.get_tracer("nexya.backend.noop")
    except ImportError:
        return _NoopTracer()


async def shutdown_otel() -> None:
    """Flush les spans en attente avant arrêt. Fail-safe."""
    global _PROVIDER
    if _PROVIDER is None:
        return
    try:
        # `shutdown()` flush + ferme les processors.
        _PROVIDER.shutdown()
    except Exception as exc:  # noqa: BLE001
        log.warning("otel.shutdown_failed", error=str(exc))


def is_initialized() -> bool:
    """Helper testable — True si OTel est actif."""
    return _INIT_OK


def _reset_for_tests() -> None:
    """Hook test-only — reset les flags pour permettre plusieurs
    setup_otel() dans la même suite pytest."""
    global _INIT_ATTEMPTED, _INIT_OK, _TRACER, _PROVIDER
    _INIT_ATTEMPTED = False
    _INIT_OK = False
    _TRACER = None
    _PROVIDER = None


# ═══════════════════════════════════════════════════════════════════
# No-op tracer (fallback si SDK absent)
# ═══════════════════════════════════════════════════════════════════


class _NoopSpan:
    def set_attribute(self, *args: Any, **kwargs: Any) -> None: ...
    def set_status(self, *args: Any, **kwargs: Any) -> None: ...
    def record_exception(self, *args: Any, **kwargs: Any) -> None: ...
    def add_event(self, *args: Any, **kwargs: Any) -> None: ...

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _NoopTracer:
    def start_as_current_span(self, *args: Any, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()

    def start_span(self, *args: Any, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()
