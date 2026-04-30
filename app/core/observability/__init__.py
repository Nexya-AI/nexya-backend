"""Infrastructure d'observabilité — logging structuré + trace_id par requête + 3 piliers K1."""

from app.core.observability.logging import configure_logging
from app.core.observability.otel import (
    get_tracer,
    setup_otel,
    shutdown_otel,
)
from app.core.observability.otel import (
    is_initialized as otel_is_initialized,
)
from app.core.observability.prometheus import (
    get_registry as prometheus_get_registry,
)
from app.core.observability.prometheus import (
    is_initialized as prometheus_is_initialized,
)
from app.core.observability.prometheus import (
    record_ai_chat_call,
    record_arq_job,
    record_cache_operation,
    record_fcm_failure,
    record_notification_dispatch,
    record_tool_execution,
    set_circuit_breaker_state,
    setup_prometheus,
    verify_scrape_token,
)
from app.core.observability.sentry import (
    is_initialized as sentry_is_initialized,
)
from app.core.observability.sentry import (
    setup_sentry,
    shutdown_sentry,
)
from app.core.observability.trace import TraceIdMiddleware, get_trace_id

__all__ = [
    "configure_logging",
    "TraceIdMiddleware",
    "get_trace_id",
    # OTel
    "setup_otel",
    "shutdown_otel",
    "get_tracer",
    "otel_is_initialized",
    # Sentry
    "setup_sentry",
    "shutdown_sentry",
    "sentry_is_initialized",
    # Prometheus
    "setup_prometheus",
    "prometheus_get_registry",
    "prometheus_is_initialized",
    "verify_scrape_token",
    "record_ai_chat_call",
    "record_tool_execution",
    "record_notification_dispatch",
    "record_fcm_failure",
    "record_arq_job",
    "set_circuit_breaker_state",
    "record_cache_operation",
]
