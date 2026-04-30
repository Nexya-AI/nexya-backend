"""Tests de l'endpoint /metrics — auth token + format Prometheus.

Vérifie :
- Sans token configuré (dev) → 200 + format text/plain valide.
- Avec token + bon header → 200.
- Avec token + bon query param → 200.
- Avec token + mauvais token → 401.
- Avec token + token absent → 401.
- Content-Type = `text/plain; version=0.0.4`.
- /observability/status retourne le JSON synthèse 3 piliers.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.observability import prometheus as p


@pytest.fixture(autouse=True)
def _reset_prom_state(monkeypatch):
    """Reset Prometheus state entre tests pour éviter la contamination."""
    p._reset_for_tests()
    p.setup_prometheus(settings)
    yield


def test_metrics_open_when_no_token_configured() -> None:
    """En dev (token vide), l'endpoint répond 200 sans auth."""
    from app.main import app

    with TestClient(app) as client:
        # Token vide par défaut en dev → endpoint ouvert
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_format_valid_prometheus() -> None:
    """Le payload contient les marqueurs Prometheus standards."""
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/metrics")
    text = response.text
    assert "# HELP nexya_ai_chat_calls_total" in text
    assert "# TYPE nexya_ai_chat_calls_total counter" in text


def test_metrics_401_with_bad_token(monkeypatch) -> None:
    """Token configuré + mauvais token fourni → 401."""
    monkeypatch.setattr(settings, "prometheus_scrape_token", "right")
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/metrics", headers={"X-Prometheus-Token": "wrong"})
    assert response.status_code == 401


def test_metrics_401_without_token_when_required(monkeypatch) -> None:
    """Token configuré + aucun token fourni → 401."""
    monkeypatch.setattr(settings, "prometheus_scrape_token", "right")
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 401


def test_metrics_200_with_correct_token_header(monkeypatch) -> None:
    """Token configuré + bon header → 200."""
    monkeypatch.setattr(settings, "prometheus_scrape_token", "right")
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/metrics", headers={"X-Prometheus-Token": "right"})
    assert response.status_code == 200


def test_metrics_200_with_correct_token_query(monkeypatch) -> None:
    """Token configuré + bon query param → 200."""
    monkeypatch.setattr(settings, "prometheus_scrape_token", "right")
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/metrics?token=right")
    assert response.status_code == 200


def test_observability_status_endpoint() -> None:
    """/observability/status retourne le JSON 3 piliers."""
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/observability/status")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert "otel" in data
    assert "sentry" in data
    assert "prometheus" in data
    assert data["otel"]["enabled"] == settings.otel_enabled
    assert data["prometheus"]["metrics_count"] == 13
    assert data["sentry"]["release"] == settings.app_version


def test_observability_status_401_with_bad_token(monkeypatch) -> None:
    """/observability/status protégé par le même token."""
    monkeypatch.setattr(settings, "prometheus_scrape_token", "right")
    from app.main import app

    with TestClient(app) as client:
        response = client.get(
            "/observability/status",
            headers={"X-Prometheus-Token": "wrong"},
        )
    assert response.status_code == 401
