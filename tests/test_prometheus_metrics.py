"""Tests des métriques Prometheus K1 — registry custom, helpers fail-safe.

Vérifie :
- setup_prometheus initialise les 13 métriques NEXYA dans un registry isolé.
- Format exposition `text/plain; version=0.0.4` valide via generate_latest.
- Counters / Histograms / Gauge incrémentent correctement avec labels.
- Helpers `record_*` sont fail-safe (no-op si pas init, log+continue si crash).
- `verify_scrape_token` constant-time : True/False selon match.
- Token vide en prod = ValueError (testé par config tests).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.core.observability import prometheus as p


@pytest.fixture(autouse=True)
def _reset_prom():
    p._reset_for_tests()
    yield
    p._reset_for_tests()


def _cfg(**overrides) -> Settings:
    base = dict(prometheus_enabled=True, prometheus_scrape_token="")
    base.update(overrides)
    return Settings(**base)


def test_setup_initializes_13_metrics() -> None:
    ok = p.setup_prometheus(_cfg())
    assert ok is True
    assert p.is_initialized() is True
    # Toutes les 13 métriques sont des objets non-None
    for name in (
        "ai_chat_calls_total",
        "ai_chat_first_chunk_seconds",
        "ai_chat_total_duration_seconds",
        "ai_tokens_consumed_total",
        "ai_cost_usd_total",
        "ai_provider_failures_total",
        "ai_circuit_breaker_state",
        "tools_executed_total",
        "tools_execution_duration_seconds",
        "notifications_dispatched_total",
        "notifications_fcm_failures_total",
        "arq_jobs_total",
        "arq_job_duration_seconds",
        "cache_operations_total",
    ):
        assert getattr(p, name) is not None, f"{name} not initialized"


def test_setup_skipped_when_kill_switch_off() -> None:
    ok = p.setup_prometheus(_cfg(prometheus_enabled=False))
    assert ok is False
    assert p.is_initialized() is False


def test_export_format_is_valid_prometheus() -> None:
    """generate_latest doit produire le format text/plain v0.0.4."""
    p.setup_prometheus(_cfg())
    from prometheus_client import generate_latest

    payload = generate_latest(p.get_registry())
    text = payload.decode("utf-8")
    assert "# HELP nexya_ai_chat_calls_total" in text
    assert "# TYPE nexya_ai_chat_calls_total counter" in text
    assert "# TYPE nexya_ai_circuit_breaker_state gauge" in text
    assert "# TYPE nexya_ai_chat_first_chunk_seconds histogram" in text


def test_record_ai_chat_call_increments_counter() -> None:
    p.setup_prometheus(_cfg())
    metrics = SimpleNamespace(
        provider="openai",
        model="gpt-4o-mini",
        outcome="success",
        expert_id="general",
        started_at=10.0,
        first_chunk_at=10.5,
        completed_at=12.0,
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        cost_usd=0.001,
        failure_code=None,
    )
    p.record_ai_chat_call(metrics)
    p.record_ai_chat_call(metrics)
    val = p.ai_chat_calls_total.labels(
        provider="openai",
        model="gpt-4o-mini",
        outcome="success",
        expert_id="general",
    )._value.get()
    assert val == 2
    # Tokens prompt + completion accumulés
    prompt_val = p.ai_tokens_consumed_total.labels(
        provider="openai", model="gpt-4o-mini", kind="prompt"
    )._value.get()
    assert prompt_val == 200
    cost_val = p.ai_cost_usd_total.labels(provider="openai", model="gpt-4o-mini")._value.get()
    assert abs(cost_val - 0.002) < 1e-9


def test_record_ai_chat_call_no_op_when_not_initialized() -> None:
    """Sans init, record_ai_chat_call est un no-op silencieux."""
    metrics = SimpleNamespace(provider="x", model="y", outcome="success", expert_id="g")
    p.record_ai_chat_call(metrics)  # ne lève pas


def test_record_tool_execution_with_labels() -> None:
    p.setup_prometheus(_cfg())
    p.record_tool_execution("create_task", True, 0.123)
    p.record_tool_execution("create_task", False, 0.456)
    succ = p.tools_executed_total.labels(name="create_task", success="true")._value.get()
    fail = p.tools_executed_total.labels(name="create_task", success="false")._value.get()
    assert succ == 1
    assert fail == 1


def test_record_notification_dispatch() -> None:
    p.setup_prometheus(_cfg())
    p.record_notification_dispatch("tasks", "push")
    p.record_notification_dispatch("tasks", "email")
    p.record_notification_dispatch("tasks", "push")
    push = p.notifications_dispatched_total.labels(
        category="tasks", channel_used="push"
    )._value.get()
    assert push == 2


def test_record_arq_job_with_histogram() -> None:
    p.setup_prometheus(_cfg())
    p.record_arq_job("dispatch_due_tasks", "completed", 0.5)
    p.record_arq_job("dispatch_due_tasks", "completed", 1.0)
    val = p.arq_jobs_total.labels(function="dispatch_due_tasks", outcome="completed")._value.get()
    assert val == 2


def test_set_circuit_breaker_state() -> None:
    p.setup_prometheus(_cfg())
    p.set_circuit_breaker_state("openai", "gpt-4o-mini", 2)
    val = p.ai_circuit_breaker_state.labels(provider="openai", model="gpt-4o-mini")._value.get()
    assert val == 2.0
    p.set_circuit_breaker_state("openai", "gpt-4o-mini", 0)
    val = p.ai_circuit_breaker_state.labels(provider="openai", model="gpt-4o-mini")._value.get()
    assert val == 0.0


def test_record_cache_operation() -> None:
    p.setup_prometheus(_cfg())
    p.record_cache_operation("get", "hit")
    p.record_cache_operation("get", "miss")
    p.record_cache_operation("put", "bypass")
    hit = p.cache_operations_total.labels(operation="get", outcome="hit")._value.get()
    miss = p.cache_operations_total.labels(operation="get", outcome="miss")._value.get()
    assert hit == 1
    assert miss == 1


def test_verify_scrape_token_constant_time() -> None:
    # Token configuré vide = endpoint ouvert
    assert p.verify_scrape_token(None, "") is True
    assert p.verify_scrape_token("anything", "") is True
    # Token configuré non-vide
    assert p.verify_scrape_token("right-token", "right-token") is True
    assert p.verify_scrape_token("wrong-token", "right-token") is False
    assert p.verify_scrape_token(None, "right-token") is False
    assert p.verify_scrape_token("", "right-token") is False


def test_record_fcm_failure_with_error_type() -> None:
    p.setup_prometheus(_cfg())
    p.record_fcm_failure("UNREGISTERED")
    p.record_fcm_failure("UNREGISTERED")
    p.record_fcm_failure("UNAVAILABLE")
    unreg = p.notifications_fcm_failures_total.labels(error_type="UNREGISTERED")._value.get()
    assert unreg == 2
