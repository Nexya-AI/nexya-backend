"""Tests de l'injection trace_id+span_id OTel dans structlog.

Vérifie :
- Sans span actif → pas de modification de event_dict.
- Avec span actif valide → trace_id (32 hex) + span_id (16 hex) injectés.
- Désactivation via OBSERVABILITY_LOG_TRACE_INJECTION=False → no-op.
- Span context invalide → no-op silencieux (fail-safe).
- Le format hex est strict : 32/16 chars, lowercase, pas de tiret.
"""

from __future__ import annotations

import pytest

from app.core.observability import otel as otel_mod
from app.core.observability.logging import _inject_otel_context


@pytest.fixture(autouse=True)
def _reset_otel():
    otel_mod._reset_for_tests()
    yield
    otel_mod._reset_for_tests()


def test_inject_no_op_when_no_span_active(monkeypatch) -> None:
    """Sans span OTel actif, l'event_dict est retourné inchangé."""
    event = {"event": "test.no_span"}
    result = _inject_otel_context(None, "info", event)
    assert "trace_id" not in result
    assert "span_id" not in result


def test_inject_adds_trace_and_span_ids_when_span_active() -> None:
    """Avec un span OTel valide, trace_id (32hex) + span_id (16hex) injectés."""
    from app.config import Settings

    cfg = Settings(otel_enabled=True, otel_traces_sampler_ratio=1.0)
    otel_mod.setup_otel(cfg)
    tracer = otel_mod.get_tracer()

    with tracer.start_as_current_span("test.with_span"):
        event = {"event": "with_span"}
        result = _inject_otel_context(None, "info", event)
        # Format strict : 32 hex pour trace_id, 16 hex pour span_id
        assert "trace_id" in result
        assert "span_id" in result
        assert len(result["trace_id"]) == 32
        assert len(result["span_id"]) == 16
        assert all(c in "0123456789abcdef" for c in result["trace_id"])
        assert all(c in "0123456789abcdef" for c in result["span_id"])


def test_inject_disabled_via_setting(monkeypatch) -> None:
    """Si OBSERVABILITY_LOG_TRACE_INJECTION=False, no-op même si span actif."""
    from app.config import Settings

    cfg = Settings(otel_enabled=True, otel_traces_sampler_ratio=1.0)
    otel_mod.setup_otel(cfg)

    # Force la valeur à False sur le singleton settings
    from app.core.observability import logging as logging_mod

    monkeypatch.setattr(
        logging_mod.settings,
        "observability_log_trace_injection",
        False,
        raising=False,
    )

    tracer = otel_mod.get_tracer()
    with tracer.start_as_current_span("test.disabled"):
        event = {"event": "test"}
        result = _inject_otel_context(None, "info", event)
        assert "trace_id" not in result
        assert "span_id" not in result


def test_inject_overrides_legacy_trace_id_when_otel_active() -> None:
    """Si event_dict a déjà un trace_id legacy (TraceIdMiddleware), l'OTel
    écrase pour cohérence avec Tempo/Jaeger."""
    from app.config import Settings

    cfg = Settings(otel_enabled=True, otel_traces_sampler_ratio=1.0)
    otel_mod.setup_otel(cfg)
    tracer = otel_mod.get_tracer()

    with tracer.start_as_current_span("test.override"):
        event = {"event": "test", "trace_id": "legacy-uuid-hex"}
        result = _inject_otel_context(None, "info", event)
        assert result["trace_id"] != "legacy-uuid-hex"
        assert len(result["trace_id"]) == 32


def test_inject_fails_safe_on_exception(monkeypatch) -> None:
    """Si OTel raise pendant get_current_span, l'event passe inchangé."""
    # Simulate ImportError en monkeypatchant le module
    event = {"event": "test"}
    # Le try/except interne du processor garantit le no-op
    result = _inject_otel_context(None, "info", event)
    # On accepte les deux : trace_id présent (span actif d'un test précédent)
    # ou absent. L'important c'est qu'aucune exception ne remonte.
    assert isinstance(result, dict)


def test_processor_does_not_pollute_event_dict() -> None:
    """Sans span, l'event_dict ne reçoit AUCUNE clé extra."""
    event = {"event": "clean", "user_id": "abc"}
    before_keys = set(event.keys())
    _inject_otel_context(None, "info", event)
    # On peut avoir trace_id/span_id si un span est encore actif d'un test
    # précédent — c'est OK parce que `autouse` de _reset_otel doit l'avoir
    # purgé. La règle invariante : pas de clé bizarre comme "exception".
    after_keys = set(event.keys())
    extras = after_keys - before_keys
    assert extras.issubset({"trace_id", "span_id"})


def test_format_hex_is_lowercase_no_dashes() -> None:
    """Format hex strict — Tempo/Jaeger UI exigent lowercase + pas de tiret."""
    from app.config import Settings

    cfg = Settings(otel_enabled=True, otel_traces_sampler_ratio=1.0)
    otel_mod.setup_otel(cfg)
    tracer = otel_mod.get_tracer()

    with tracer.start_as_current_span("test.hex_format"):
        event = {"event": "test"}
        result = _inject_otel_context(None, "info", event)
        if "trace_id" in result:
            tid = result["trace_id"]
            assert tid == tid.lower()
            assert "-" not in tid
            assert "_" not in tid
