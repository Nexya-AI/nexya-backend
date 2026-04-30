"""Tests d'init OpenTelemetry — setup, kill-switch, fail-safe.

Vérifie :
- `OTEL_ENABLED=False` → no-op total (pas de TracerProvider configuré).
- `OTEL_ENABLED=True` → init OK + tracer fonctionnel.
- Sampler 0.0 / 1.0 — bornes extrêmes acceptées.
- Fail-safe sur exceptions internes : le service continue.
- `get_tracer()` retourne toujours un objet utilisable, même sans init.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.observability import otel as otel_mod


@pytest.fixture(autouse=True)
def _reset_otel():
    """Reset module-level state AVANT et APRÈS chaque test."""
    otel_mod._reset_for_tests()
    yield
    otel_mod._reset_for_tests()


def _cfg(**overrides) -> Settings:
    base = dict(
        otel_enabled=True,
        otel_exporter_otlp_endpoint="http://127.0.0.1:65530",
        otel_service_name="nexya-test",
        otel_traces_sampler_ratio=1.0,
        app_version="dev",
        sentry_environment="development",
    )
    base.update(overrides)
    return Settings(**base)


def test_otel_disabled_when_kill_switch_off() -> None:
    """OTEL_ENABLED=False → setup_otel retourne False, no-op total."""
    cfg = _cfg(otel_enabled=False)
    ok = otel_mod.setup_otel(cfg)
    assert ok is False
    assert otel_mod.is_initialized() is False


def test_otel_init_succeeds_without_app_or_engine() -> None:
    """Init minimal sans FastAPI app ni DB engine — doit fonctionner."""
    cfg = _cfg()
    ok = otel_mod.setup_otel(cfg)
    assert ok is True
    assert otel_mod.is_initialized() is True


def test_otel_idempotent_init() -> None:
    """Deux setup_otel() de suite — deuxième est no-op."""
    cfg = _cfg()
    assert otel_mod.setup_otel(cfg) is True
    # Second appel — l'état flag empêche la ré-init
    assert otel_mod.setup_otel(cfg) is True
    assert otel_mod.is_initialized() is True


def test_get_tracer_returns_noop_when_uninitialized() -> None:
    """Sans init, get_tracer() retourne un objet utilisable (no-op)."""
    tracer = otel_mod.get_tracer()
    # On peut start un span sans crasher
    with tracer.start_as_current_span("test.noop") as span:
        # set_attribute doit être un no-op silencieux
        if hasattr(span, "set_attribute"):
            span.set_attribute("foo", "bar")


def test_get_tracer_returns_real_tracer_after_init() -> None:
    """Après init, get_tracer() retourne un Tracer OTel réel."""
    cfg = _cfg()
    otel_mod.setup_otel(cfg)
    tracer = otel_mod.get_tracer()
    with tracer.start_as_current_span("test.real") as span:
        span.set_attribute("ai.expert_id", "general")
        span.set_attribute("ai.outcome", "success")


def test_otel_sampler_zero_ratio() -> None:
    """sampler_ratio=0.0 — init OK, mais tous les spans seront drop."""
    cfg = _cfg(otel_traces_sampler_ratio=0.0)
    ok = otel_mod.setup_otel(cfg)
    assert ok is True


def test_otel_sampler_full_ratio() -> None:
    """sampler_ratio=1.0 — init OK, tous les spans sont sampled."""
    cfg = _cfg(otel_traces_sampler_ratio=1.0)
    ok = otel_mod.setup_otel(cfg)
    assert ok is True


def test_otel_init_with_unreachable_endpoint() -> None:
    """Endpoint inaccessible — init succède quand même (fail-open silent).

    Le BatchSpanProcessor exporte en async, donc l'endpoint ne bloque
    pas l'init. Un seul warning au boot, le service tourne.
    """
    cfg = _cfg(otel_exporter_otlp_endpoint="http://invalid-host-xyz:9999")
    ok = otel_mod.setup_otel(cfg)
    assert ok is True


@pytest.mark.asyncio
async def test_shutdown_otel_idempotent() -> None:
    """shutdown_otel sans init préalable — pas de crash."""
    await otel_mod.shutdown_otel()  # ne lève pas
    cfg = _cfg()
    otel_mod.setup_otel(cfg)
    await otel_mod.shutdown_otel()  # premier vrai shutdown
    await otel_mod.shutdown_otel()  # second — idempotent
