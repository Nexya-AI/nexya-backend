"""Tests des settings K1 — observabilité prod (OTel + Sentry + Prometheus).

Vérifie :
- Les 14 settings ont des valeurs par défaut sécurisées (off par défaut sauf Prometheus).
- Les bornes Pydantic (ratio ∈ [0, 1], pattern environment) rejettent les valeurs invalides.
- Le `_enforce_production_safety` model_validator fail-fast en prod si
  `PROMETHEUS_SCRAPE_TOKEN` est vide.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _base_prod_kwargs(**overrides):
    """Settings minimaux qui passent la validation prod (sauf l'override testé)."""
    base = {
        "env": "production",
        "app_secret": "x" * 64,
        "debug": False,
        "db_echo": False,
        "allowed_origins": "https://app.nexya.ai",
        "jwt_private_key": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----",
        "jwt_public_key": "-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----",
        "prometheus_scrape_token": "secret-scrape-token",
        # K2 — production safety guard refuse vide/"admin" sur Grafana
        "grafana_admin_password": "strong-admin-password-32+chars-x",
        # J1 — liste admin RGPD non-vide obligatoire en prod
        "rgpd_admin_emails": ["dpo@nexya.ai"],
        # O1 — preset headers sécurité prod obligatoire en prod
        "security_headers_preset": "prod",
        # E4.5 — C2PA désactivé par défaut dans tests (évite besoin clés X.509)
        "c2pa_enabled": False,
    }
    base.update(overrides)
    return base


def test_observability_defaults_secure() -> None:
    """OTel + Sentry off par défaut, Prometheus on (mais token vide)."""
    s = Settings()
    assert s.otel_enabled is False  # nécessite OTLP collector
    assert s.sentry_dsn == ""  # nécessite compte Sentry
    assert s.prometheus_enabled is True
    assert s.prometheus_scrape_token == ""
    assert s.app_version == "dev"
    assert s.observability_log_trace_injection is True
    assert s.otel_traces_sampler_ratio == 0.1
    assert s.sentry_traces_sample_rate == 0.05
    assert s.sentry_profiles_sample_rate == 0.0


def test_k2_grafana_password_required_strong_in_production() -> None:
    """K2 — production safety guard refuse 'admin' ou vide."""
    for weak in ("", "admin"):
        kwargs = _base_prod_kwargs(grafana_admin_password=weak)
        with pytest.raises(ValidationError) as excinfo:
            Settings(**kwargs)
        assert "GRAFANA_ADMIN_PASSWORD" in str(excinfo.value)


def test_k2_grafana_password_strong_passes_in_production() -> None:
    """K2 — un mot de passe fort passe la validation prod."""
    kwargs = _base_prod_kwargs(grafana_admin_password="strong-admin-pwd-x-y-z")
    s = Settings(**kwargs)
    assert s.grafana_admin_password.startswith("strong-")


def test_k2_cost_threshold_default_provisional() -> None:
    """K2 — seuil USD/jour par défaut 100, à valider par Ivan."""
    s = Settings()
    assert s.cost_usd_daily_alert_threshold == 100.0
    assert s.prometheus_scrape_interval_seconds == 15


def test_otel_traces_sampler_ratio_bounds() -> None:
    """Ratio ∈ [0, 1] strict."""
    Settings(otel_traces_sampler_ratio=0.0)
    Settings(otel_traces_sampler_ratio=1.0)
    with pytest.raises(ValidationError):
        Settings(otel_traces_sampler_ratio=1.5)
    with pytest.raises(ValidationError):
        Settings(otel_traces_sampler_ratio=-0.1)


def test_sentry_environment_pattern() -> None:
    """sentry_environment doit être development|staging|production."""
    Settings(sentry_environment="development")
    Settings(sentry_environment="staging")
    Settings(sentry_environment="production")
    with pytest.raises(ValidationError):
        Settings(sentry_environment="prod")  # raccourci interdit


def test_prometheus_metrics_path_pattern() -> None:
    """Le path doit commencer par / et contenir [\\w/-] uniquement."""
    Settings(prometheus_metrics_path="/metrics")
    Settings(prometheus_metrics_path="/internal/metrics")
    with pytest.raises(ValidationError):
        Settings(prometheus_metrics_path="metrics")  # missing leading /
    with pytest.raises(ValidationError):
        Settings(prometheus_metrics_path="/metrics?token=x")  # ? interdit


def test_app_version_length_bounds() -> None:
    """app_version ∈ [1, 128] chars."""
    Settings(app_version="x")
    Settings(app_version="v" + "0" * 127)
    with pytest.raises(ValidationError):
        Settings(app_version="")
    with pytest.raises(ValidationError):
        Settings(app_version="v" + "0" * 128)


def test_production_requires_prometheus_token() -> None:
    """En prod, PROMETHEUS_SCRAPE_TOKEN vide doit lever ValueError."""
    kwargs = _base_prod_kwargs(prometheus_scrape_token="")
    with pytest.raises(ValueError, match="PROMETHEUS_SCRAPE_TOKEN"):
        Settings(**kwargs)


def test_production_with_token_passes() -> None:
    """En prod avec token rempli, la validation passe."""
    kwargs = _base_prod_kwargs(prometheus_scrape_token="abcdef-1234")
    s = Settings(**kwargs)
    assert s.prometheus_scrape_token == "abcdef-1234"


def test_production_prometheus_disabled_no_token_required() -> None:
    """Si prometheus_enabled=False, le token vide est toléré en prod."""
    kwargs = _base_prod_kwargs(prometheus_scrape_token="", prometheus_enabled=False)
    s = Settings(**kwargs)
    assert s.prometheus_enabled is False
