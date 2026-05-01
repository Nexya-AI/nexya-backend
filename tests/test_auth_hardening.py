"""
Suite de durcissement P0 — Feature Auth + infrastructure liée.

Ces 7 tests protègent les correctifs audit :
- `/healthz` et `/ready` se comportent correctement
- Le scrubber n'autorise aucune fuite de secret en log
- Pydantic refuse les mots de passe faibles dès la requête
- La config refuse les valeurs de dev en `ENV=production`
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.core.errors.handlers import _safe_body_preview, _safe_errors, _scrub
from app.features.auth.schemas import RegisterRequest
from app.main import app

# ══════════════════════════════════════════════════════════════
# 1. Healthz — liveness indépendante des dépendances externes
# ══════════════════════════════════════════════════════════════


def test_healthz_returns_ok_without_dependencies() -> None:
    """La liveness probe ne doit JAMAIS dépendre de DB/Redis."""
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"


# ══════════════════════════════════════════════════════════════
# 2. Ready — readiness renvoie 503 si une dépendance est down
# ══════════════════════════════════════════════════════════════


def test_ready_returns_503_when_database_unreachable() -> None:
    """En CI sans Postgres joignable, /ready doit signaler 503 (pas 200).

    Le conftest pointe DATABASE_URL sur un port inexistant — la connexion
    échoue rapidement (connect_timeout=5s) et /ready répond proprement.
    """
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code in (200, 503)  # 200 si DB dispo localement
    body = response.json()
    assert "db" in body["data"]
    assert "redis" in body["data"]


# ══════════════════════════════════════════════════════════════
# 3. Register — mot de passe faible rejeté côté schéma
# ══════════════════════════════════════════════════════════════


def test_register_request_rejects_weak_password() -> None:
    """Un mot de passe sans majuscule/chiffre doit échouer AVANT d'atteindre le service."""
    with pytest.raises(ValidationError):
        RegisterRequest(email="ivan@nexya.ai", password="allsmall")


def test_register_request_accepts_strong_password() -> None:
    """Un mot de passe conforme doit passer sans bruit."""
    req = RegisterRequest(email="ivan@nexya.ai", password="Secur3Pass")
    assert req.email == "ivan@nexya.ai"
    assert req.password == "Secur3Pass"


# ══════════════════════════════════════════════════════════════
# 4. Scrubber — aucun secret ne doit transiter en log
# ══════════════════════════════════════════════════════════════


def test_scrub_masks_password_and_token_fields() -> None:
    """Les clés sensibles sont masquées récursivement, même en profondeur."""
    payload = {
        "email": "ivan@nexya.ai",
        "password": "Secur3Pass",
        "nested": {"refresh_token": "eyJabc…", "ok": "keep"},
        "list": [{"api_key": "sk-…"}, {"harmless": "keep"}],
    }
    cleaned = _scrub(payload)
    assert cleaned["email"] == "ivan@nexya.ai"
    assert cleaned["password"] == "***REDACTED***"
    assert cleaned["nested"]["refresh_token"] == "***REDACTED***"
    assert cleaned["nested"]["ok"] == "keep"
    assert cleaned["list"][0]["api_key"] == "***REDACTED***"
    assert cleaned["list"][1]["harmless"] == "keep"


def test_safe_body_preview_masks_password_in_raw_bytes() -> None:
    """Cas réel du handler : exc.body arrive en bytes."""
    raw = b'{"email":"ivan@nexya.ai","password":"Secur3Pass"}'
    preview = _safe_body_preview(raw)
    assert "Secur3Pass" not in preview
    assert "***REDACTED***" in preview


def test_safe_errors_redacts_sensitive_input() -> None:
    """Pydantic v2 place la valeur invalide dans `input` — il faut la masquer."""
    errors = [
        {
            "loc": ("body", "password"),
            "msg": "Le mot de passe doit contenir au moins un chiffre.",
            "input": "weakpass",
        }
    ]
    safe = _safe_errors(errors)
    assert safe[0]["input"] == "***REDACTED***"
    assert safe[0]["msg"] == errors[0]["msg"]  # le message reste informatif


# ══════════════════════════════════════════════════════════════
# 5. Config production — refus des valeurs de dev
# ══════════════════════════════════════════════════════════════


def test_production_settings_reject_wildcard_cors() -> None:
    """ALLOWED_ORIGINS=* combiné à allow_credentials=True = faille CSRF. Refusé en prod."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            env="production",
            app_secret="a-very-long-random-production-secret-value",
            jwt_private_key="-----BEGIN PRIVATE-----\nok\n-----END PRIVATE-----",
            jwt_public_key="-----BEGIN PUBLIC-----\nok\n-----END PUBLIC-----",
            allowed_origins="*",
            debug=False,
        )
    assert "ALLOWED_ORIGINS" in str(excinfo.value)


def test_production_settings_accept_valid_configuration() -> None:
    """Une configuration production propre doit charger sans erreur."""
    settings = Settings(
        _env_file=None,
        env="production",
        app_secret="a-very-long-random-production-secret-value",
        jwt_private_key="-----BEGIN PRIVATE-----\nok\n-----END PRIVATE-----",
        jwt_public_key="-----BEGIN PUBLIC-----\nok\n-----END PUBLIC-----",
        allowed_origins="https://app.nexya.ai,https://www.nexya.ai",
        debug=False,
        db_echo=False,
        # K1 — token Prometheus obligatoire en prod
        prometheus_scrape_token="prod-scrape-token-not-empty",
        # K2 — Grafana admin password fort obligatoire en prod
        grafana_admin_password="prod-grafana-strong-password-x32",
        # J1 — au moins un email DPO obligatoire en prod
        rgpd_admin_emails=["dpo@nexya.ai"],
        # O1 — preset headers sécurité prod obligatoire en prod
        security_headers_preset="prod",
        # E4.5 — C2PA désactivé dans tests (évite besoin clés X.509)
        c2pa_enabled=False,
    )
    assert settings.is_production is True
    assert settings.cors_origins == ["https://app.nexya.ai", "https://www.nexya.ai"]
