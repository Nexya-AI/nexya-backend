"""
Prometheus — métriques applicatives NEXYA.

Pattern :
- Registry custom (pas le `REGISTRY` global) → isolation totale dans
  les tests (chaque test peut reset son propre registry sans
  contaminer les autres).
- 13 métriques NEXYA custom couvrant les KPI critiques :
  IA (calls, TTFB, durée, tokens, coût USD, échecs, breaker state),
  tools (executions, durée), notifications (dispatch, FCM failures),
  workers arq (jobs, durée), cache prompt (hits/miss/bypass).
- Helpers fail-safe (`record_*`) : si la métrique n'est pas
  initialisée OU si l'incrément lève (cardinalité explosée…), on
  log un warning unique et on continue. Une instrumentation KO ne
  doit JAMAIS bloquer un endpoint user.
- Endpoint /metrics monté côté `main.py`, auth via
  `PROMETHEUS_SCRAPE_TOKEN` (constant-time compare) + format
  exposition `text/plain; version=0.0.4`.

Buckets Histogram adaptés à NEXYA (latence Africa 2G/3G) :
50ms → 60s. Les seuils permettent de capturer la queue p99 sans
exploser la cardinalité.
"""

from __future__ import annotations

import hmac
from typing import Any

import structlog

from app.config import Settings, settings

log = structlog.get_logger(__name__)


# Buckets latence en secondes — adaptés au contexte Africa 2G/3G.
# La p50 doit tomber dans 100-500ms, la p99 ne devrait pas dépasser
# 30s (sauf streams chat qui peuvent durer 2min, captés par le
# bucket +Inf).
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, float("inf"))


# ═══════════════════════════════════════════════════════════════════
# Registry singleton + métriques module-level
# ═══════════════════════════════════════════════════════════════════


_REGISTRY: Any = None
_INIT_ATTEMPTED = False
_INIT_OK = False

# Les 13 métriques NEXYA — initialisées par setup_prometheus().
ai_chat_calls_total: Any = None
ai_chat_first_chunk_seconds: Any = None
ai_chat_total_duration_seconds: Any = None
ai_tokens_consumed_total: Any = None
ai_cost_usd_total: Any = None
ai_provider_failures_total: Any = None
ai_circuit_breaker_state: Any = None
tools_executed_total: Any = None
tools_execution_duration_seconds: Any = None
notifications_dispatched_total: Any = None
notifications_fcm_failures_total: Any = None
arq_jobs_total: Any = None
arq_job_duration_seconds: Any = None
cache_operations_total: Any = None


def setup_prometheus(cfg: Settings | None = None) -> bool:
    """Initialise le registry + les 13 métriques NEXYA.

    Fail-fast en prod si `PROMETHEUS_SCRAPE_TOKEN` est vide
    (l'endpoint /metrics non-protégé fuiterait les KPI métier).
    En dev avec token vide, log un warning unique au boot puis
    continue (endpoint ouvert, OK pour itérer en local).
    """
    global _INIT_ATTEMPTED, _INIT_OK, _REGISTRY
    global ai_chat_calls_total, ai_chat_first_chunk_seconds
    global ai_chat_total_duration_seconds, ai_tokens_consumed_total
    global ai_cost_usd_total, ai_provider_failures_total
    global ai_circuit_breaker_state, tools_executed_total
    global tools_execution_duration_seconds, notifications_dispatched_total
    global notifications_fcm_failures_total, arq_jobs_total
    global arq_job_duration_seconds, cache_operations_total

    cfg = cfg or settings
    if _INIT_ATTEMPTED:
        return _INIT_OK
    _INIT_ATTEMPTED = True

    if not cfg.prometheus_enabled:
        log.info("prometheus.disabled", reason="kill_switch_off")
        return False

    # Le model_validator de Settings a déjà fail-fast en prod si le
    # token est vide. Ici on log juste un warning en dev pour
    # signaler la posture ouverte.
    if not cfg.prometheus_scrape_token:
        log.warning(
            "prometheus.metrics_open",
            hint="/metrics ouvert sans token - OK en dev, JAMAIS en prod",
        )

    try:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
        )
    except ImportError as exc:
        log.warning("prometheus.import_failed", error=str(exc))
        return False

    try:
        _REGISTRY = CollectorRegistry()

        ai_chat_calls_total = Counter(
            "nexya_ai_chat_calls_total",
            "Nombre total d'appels chat LLM par provider/model/outcome/expert.",
            labelnames=("provider", "model", "outcome", "expert_id"),
            registry=_REGISTRY,
        )
        ai_chat_first_chunk_seconds = Histogram(
            "nexya_ai_chat_first_chunk_seconds",
            "Time-To-First-Byte (TTFB) du stream chat par provider/model.",
            labelnames=("provider", "model"),
            buckets=LATENCY_BUCKETS,
            registry=_REGISTRY,
        )
        ai_chat_total_duration_seconds = Histogram(
            "nexya_ai_chat_total_duration_seconds",
            "Durée totale d'un stream chat par provider/model.",
            labelnames=("provider", "model"),
            buckets=LATENCY_BUCKETS,
            registry=_REGISTRY,
        )
        ai_tokens_consumed_total = Counter(
            "nexya_ai_tokens_consumed_total",
            "Tokens consommés (prompt + completion) par provider/model/kind.",
            labelnames=("provider", "model", "kind"),
            registry=_REGISTRY,
        )
        ai_cost_usd_total = Counter(
            "nexya_ai_cost_usd_total",
            "Coût USD cumulé par provider/model.",
            labelnames=("provider", "model"),
            registry=_REGISTRY,
        )
        ai_provider_failures_total = Counter(
            "nexya_ai_provider_failures_total",
            "Échecs provider par provider/model/error_type.",
            labelnames=("provider", "model", "error_type"),
            registry=_REGISTRY,
        )
        ai_circuit_breaker_state = Gauge(
            "nexya_ai_circuit_breaker_state",
            "État du circuit breaker (0=closed, 1=half_open, 2=open).",
            labelnames=("provider", "model"),
            registry=_REGISTRY,
        )
        tools_executed_total = Counter(
            "nexya_tools_executed_total",
            "Exécutions de tools LLM par nom/success.",
            labelnames=("name", "success"),
            registry=_REGISTRY,
        )
        tools_execution_duration_seconds = Histogram(
            "nexya_tools_execution_duration_seconds",
            "Durée d'exécution d'un tool par nom.",
            labelnames=("name",),
            buckets=LATENCY_BUCKETS,
            registry=_REGISTRY,
        )
        notifications_dispatched_total = Counter(
            "nexya_notifications_dispatched_total",
            "Notifications dispatchées par catégorie/canal utilisé.",
            labelnames=("category", "channel_used"),
            registry=_REGISTRY,
        )
        notifications_fcm_failures_total = Counter(
            "nexya_notifications_fcm_failures_total",
            "Échecs FCM (push) par type d'erreur.",
            labelnames=("error_type",),
            registry=_REGISTRY,
        )
        arq_jobs_total = Counter(
            "nexya_arq_jobs_total",
            "Jobs arq exécutés par fonction/outcome.",
            labelnames=("function", "outcome"),
            registry=_REGISTRY,
        )
        arq_job_duration_seconds = Histogram(
            "nexya_arq_job_duration_seconds",
            "Durée d'exécution d'un job arq par fonction.",
            labelnames=("function",),
            buckets=LATENCY_BUCKETS,
            registry=_REGISTRY,
        )
        cache_operations_total = Counter(
            "nexya_cache_operations_total",
            "Opérations sur le cache prompt (op=get|put, outcome=hit|miss|bypass|error).",
            labelnames=("operation", "outcome"),
            registry=_REGISTRY,
        )

        _INIT_OK = True
        log.info(
            "prometheus.initialized",
            metrics_count=13,
            metrics_path=cfg.prometheus_metrics_path,
            token_protected=bool(cfg.prometheus_scrape_token),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "prometheus.init_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


def get_registry() -> Any:
    """Retourne le registry custom (ou None si Prometheus inactif)."""
    return _REGISTRY


def is_initialized() -> bool:
    """Helper testable — True si Prometheus est actif."""
    return _INIT_OK


def _reset_for_tests() -> None:
    """Hook test-only — reset complet du module pour permettre
    plusieurs setup_prometheus() dans la même suite pytest."""
    global _INIT_ATTEMPTED, _INIT_OK, _REGISTRY
    global ai_chat_calls_total, ai_chat_first_chunk_seconds
    global ai_chat_total_duration_seconds, ai_tokens_consumed_total
    global ai_cost_usd_total, ai_provider_failures_total
    global ai_circuit_breaker_state, tools_executed_total
    global tools_execution_duration_seconds, notifications_dispatched_total
    global notifications_fcm_failures_total, arq_jobs_total
    global arq_job_duration_seconds, cache_operations_total

    _INIT_ATTEMPTED = False
    _INIT_OK = False
    _REGISTRY = None
    ai_chat_calls_total = None
    ai_chat_first_chunk_seconds = None
    ai_chat_total_duration_seconds = None
    ai_tokens_consumed_total = None
    ai_cost_usd_total = None
    ai_provider_failures_total = None
    ai_circuit_breaker_state = None
    tools_executed_total = None
    tools_execution_duration_seconds = None
    notifications_dispatched_total = None
    notifications_fcm_failures_total = None
    arq_jobs_total = None
    arq_job_duration_seconds = None
    cache_operations_total = None


# ═══════════════════════════════════════════════════════════════════
# Helpers d'enregistrement — fail-safe absolu
# ═══════════════════════════════════════════════════════════════════


def _safe_call(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "prometheus.record_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )


def record_ai_chat_call(metrics: Any) -> None:
    """Hook depuis `StreamHandler._persist_call` — enregistre les 4
    métriques chat (calls, TTFB, durée totale, tokens, coût, failure).

    `metrics` = StreamMetrics (duck-typing pour éviter l'import
    circulaire avec `app.ai.observability`).
    """
    if not _INIT_OK or metrics is None:
        return

    provider = getattr(metrics, "provider", None) or "unknown"
    model = getattr(metrics, "model", None) or "unknown"
    outcome = getattr(metrics, "outcome", None) or "unknown"
    expert_id = getattr(metrics, "expert_id", None) or "general"

    _safe_call(
        ai_chat_calls_total.labels(
            provider=provider, model=model, outcome=outcome, expert_id=expert_id
        ).inc
    )

    started_at = getattr(metrics, "started_at", None)
    first_chunk_at = getattr(metrics, "first_chunk_at", None)
    completed_at = getattr(metrics, "completed_at", None)

    if started_at is not None and first_chunk_at is not None:
        ttfb = max(0.0, float(first_chunk_at - started_at))
        _safe_call(
            ai_chat_first_chunk_seconds.labels(provider=provider, model=model).observe,
            ttfb,
        )

    if started_at is not None and completed_at is not None:
        duration = max(0.0, float(completed_at - started_at))
        _safe_call(
            ai_chat_total_duration_seconds.labels(provider=provider, model=model).observe,
            duration,
        )

    usage = getattr(metrics, "usage", None)
    if usage is not None:
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        if prompt_tokens:
            _safe_call(
                ai_tokens_consumed_total.labels(provider=provider, model=model, kind="prompt").inc,
                prompt_tokens,
            )
        if completion_tokens:
            _safe_call(
                ai_tokens_consumed_total.labels(
                    provider=provider, model=model, kind="completion"
                ).inc,
                completion_tokens,
            )

    cost = getattr(metrics, "cost_usd", None)
    if cost is not None and float(cost) > 0:
        _safe_call(
            ai_cost_usd_total.labels(provider=provider, model=model).inc,
            float(cost),
        )

    if outcome == "failed":
        failure_code = getattr(metrics, "failure_code", None) or "UNKNOWN"
        _safe_call(
            ai_provider_failures_total.labels(
                provider=provider, model=model, error_type=failure_code
            ).inc
        )


def record_tool_execution(name: str, success: bool, duration_s: float) -> None:
    """Hook depuis `execute_tool_call`."""
    if not _INIT_OK:
        return
    _safe_call(tools_executed_total.labels(name=name, success="true" if success else "false").inc)
    if duration_s > 0:
        _safe_call(
            tools_execution_duration_seconds.labels(name=name).observe,
            duration_s,
        )


def record_notification_dispatch(category: str, channel_used: str) -> None:
    """Hook depuis `NotificationDispatcher.dispatch`."""
    if not _INIT_OK:
        return
    _safe_call(
        notifications_dispatched_total.labels(category=category, channel_used=channel_used).inc
    )


def record_fcm_failure(error_type: str) -> None:
    """Hook depuis `_try_push` quand FCM échoue."""
    if not _INIT_OK:
        return
    _safe_call(notifications_fcm_failures_total.labels(error_type=error_type).inc)


def record_arq_job(function: str, outcome: str, duration_s: float) -> None:
    """Hook depuis le middleware before_job/after_job arq."""
    if not _INIT_OK:
        return
    _safe_call(arq_jobs_total.labels(function=function, outcome=outcome).inc)
    if duration_s > 0:
        _safe_call(
            arq_job_duration_seconds.labels(function=function).observe,
            duration_s,
        )


def set_circuit_breaker_state(provider: str, model: str, state: int) -> None:
    """Hook depuis `CircuitBreaker` sur transition d'état.

    Convention : 0=closed, 1=half_open, 2=open.
    """
    if not _INIT_OK:
        return
    _safe_call(
        ai_circuit_breaker_state.labels(provider=provider, model=model).set,
        float(state),
    )


def record_cache_operation(operation: str, outcome: str) -> None:
    """Hook depuis `PromptCache.get` / `PromptCache.put`.

    `operation` ∈ {get, put}, `outcome` ∈ {hit, miss, bypass, error}.
    """
    if not _INIT_OK:
        return
    _safe_call(cache_operations_total.labels(operation=operation, outcome=outcome).inc)


# ═══════════════════════════════════════════════════════════════════
# Auth helper /metrics — comparaison constant-time
# ═══════════════════════════════════════════════════════════════════


def verify_scrape_token(provided: str | None, expected: str | None) -> bool:
    """Compare deux tokens en temps constant.

    - Token configuré vide → endpoint ouvert (OK en dev avec warning
      au boot, refusé en prod par le model_validator de Settings).
    - Token configuré non-vide → exige une correspondance exacte.
    """
    if not expected:
        return True  # endpoint ouvert (validé en dev seulement)
    if not provided:
        return False
    return hmac.compare_digest(str(provided).encode(), str(expected).encode())
