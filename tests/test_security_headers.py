"""
Tests O1 — `NexyaSecurityHeadersMiddleware` (volet C).

Couvre :
1. Preset `off` → aucun header posé.
2. Preset `dev` → seul `X-Content-Type-Options: nosniff`.
3. Preset `staging` → CSP `unsafe-inline` + HSTS court + X-Frame + Referrer + Permissions.
4. Preset `prod` → CSP strict + HSTS preload + COOP + CORP.
5. Skip CSP sur `/docs` quand preset != prod.
6. Skip CSP sur `/redoc` quand preset != prod.
7. Skip CSP sur `/openapi.json` quand preset != prod.
8. Prod ne skip pas (CSP appliqué partout).
9. Middleware ne casse pas le content de la réponse.
10. Middleware ne casse pas le status_code.
11. `_enforce_production_safety` rejette `dev` en prod.
12. `_enforce_production_safety` accepte `off` en prod (kill-switch).
13. Constructor refuse preset inconnu.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security.headers import (
    NexyaSecurityHeadersMiddleware,
)

# ══════════════════════════════════════════════════════════════
# Helper — fabrique d'app FastAPI minimale avec preset configurable
# ══════════════════════════════════════════════════════════════


def _make_app(preset: str) -> FastAPI:
    fast_app = FastAPI()
    fast_app.add_middleware(NexyaSecurityHeadersMiddleware, preset=preset)

    @fast_app.get("/healthz")
    def _healthz():
        return {"ok": True}

    @fast_app.get("/docs")
    def _docs():
        return {"swagger": "fake"}

    @fast_app.get("/redoc")
    def _redoc():
        return {"redoc": "fake"}

    @fast_app.get("/openapi.json")
    def _openapi():
        return {"openapi": "fake"}

    return fast_app


# ══════════════════════════════════════════════════════════════
# Presets
# ══════════════════════════════════════════════════════════════


def test_preset_off_poses_no_headers() -> None:
    app = _make_app("off")
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert "X-Content-Type-Options" not in resp.headers
    assert "Strict-Transport-Security" not in resp.headers
    assert "Content-Security-Policy" not in resp.headers


def test_preset_dev_poses_only_nosniff() -> None:
    app = _make_app("dev")
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Strict-Transport-Security" not in resp.headers
    assert "Content-Security-Policy" not in resp.headers


def test_preset_staging_poses_csp_hsts_xframe_referrer() -> None:
    app = _make_app("staging")
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "max-age=31536000" in resp.headers.get("Strict-Transport-Security", "")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self' 'unsafe-inline'" in csp
    # Staging ne doit PAS avoir preload (engagement long terme)
    assert "preload" not in resp.headers.get("Strict-Transport-Security", "")


def test_preset_prod_poses_strict_csp_hsts_preload_coop_corp() -> None:
    app = _make_app("prod")
    with TestClient(app) as client:
        resp = client.get("/healthz")
    hsts = resp.headers.get("Strict-Transport-Security", "")
    assert "max-age=31536000" in hsts
    assert "includeSubDomains" in hsts
    assert "preload" in hsts
    assert resp.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert resp.headers.get("Cross-Origin-Resource-Policy") == "same-origin"
    csp = resp.headers.get("Content-Security-Policy", "")
    # CSP prod = STRICT (sans unsafe-inline)
    assert "'unsafe-inline'" not in csp
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("Permissions-Policy", "").startswith("camera=()")


# ══════════════════════════════════════════════════════════════
# Skip CSP sur Swagger paths
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_dev_skips_csp_on_swagger_paths(path: str) -> None:
    app = _make_app("staging")  # staging a une CSP
    with TestClient(app) as client:
        resp = client.get(path)
    # CSP doit être ABSENT sur les paths Swagger en non-prod
    assert "Content-Security-Policy" not in resp.headers
    # Mais les autres headers staging restent posés
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_prod_does_not_skip_csp_on_swagger_paths(path: str) -> None:
    """En prod, Swagger UI n'est pas exposé → on applique la CSP partout."""
    app = _make_app("prod")
    with TestClient(app) as client:
        resp = client.get(path)
    assert "Content-Security-Policy" in resp.headers


# ══════════════════════════════════════════════════════════════
# Idempotence + non-corruption response
# ══════════════════════════════════════════════════════════════


def test_middleware_does_not_modify_status_code() -> None:
    app = _make_app("staging")
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200


def test_middleware_does_not_modify_body() -> None:
    app = _make_app("staging")
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.json() == {"ok": True}


def test_middleware_does_not_overwrite_existing_header() -> None:
    """Si l'endpoint a déjà posé un header avec le même nom, on respecte."""
    fast_app = FastAPI()
    fast_app.add_middleware(NexyaSecurityHeadersMiddleware, preset="staging")

    @fast_app.get("/custom")
    def _custom():
        from fastapi.responses import JSONResponse

        return JSONResponse(
            content={"ok": True},
            headers={"X-Frame-Options": "SAMEORIGIN"},
        )

    with TestClient(fast_app) as client:
        resp = client.get("/custom")
    # Le header custom de l'endpoint est respecté
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"


# ══════════════════════════════════════════════════════════════
# Constructor + production safety
# ══════════════════════════════════════════════════════════════


def test_constructor_refuses_unknown_preset() -> None:
    fast_app = FastAPI()
    with pytest.raises(ValueError, match="Preset inconnu"):
        NexyaSecurityHeadersMiddleware(fast_app, preset="strict-mega")


def test_production_safety_rejects_dev_preset_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`is_production AND security_headers_preset='dev'` → ValueError."""
    from app.config import Settings

    # On construit Settings avec env=production + preset=dev → doit raise
    base_kwargs = _base_prod_kwargs()
    base_kwargs["security_headers_preset"] = "dev"
    with pytest.raises(ValueError, match="SECURITY_HEADERS_PRESET"):
        Settings(**base_kwargs)


def test_production_safety_accepts_off_preset_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`off` est accepté en prod (kill-switch incident)."""
    from app.config import Settings

    base_kwargs = _base_prod_kwargs()
    base_kwargs["security_headers_preset"] = "off"
    settings = Settings(**base_kwargs)
    assert settings.security_headers_preset == "off"


def test_production_safety_accepts_prod_preset_in_prod() -> None:
    from app.config import Settings

    base_kwargs = _base_prod_kwargs()
    base_kwargs["security_headers_preset"] = "prod"
    settings = Settings(**base_kwargs)
    assert settings.security_headers_preset == "prod"


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _base_prod_kwargs() -> dict:
    """Settings minimaux pour un env=production qui passe les autres garde-fous."""
    return {
        "env": "production",
        "app_secret": "prod-secret-very-long-and-strong-not-default-value-12345",
        "database_url": "postgresql+psycopg://nexya:strong@db:5432/nexya",
        "redis_url": "redis://redis:6379/0",
        "allowed_origins": "https://app.nexya.ai",
        "jwt_private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        "jwt_public_key": "-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----",
        "debug": False,
        "db_echo": False,
        "prometheus_enabled": True,
        "prometheus_scrape_token": "prod-scrape-token-32-chars-minimum-secret",
        "grafana_admin_password": "strong-grafana-password-for-prod",
        "rgpd_admin_emails": ["dpo@nexya.ai"],
        # E4.5 — C2PA désactivé dans tests (évite besoin clés X.509)
        "c2pa_enabled": False,
    }
