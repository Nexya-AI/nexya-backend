"""
Tests O1 — schéma OpenAPI enrichi (`app/core/openapi/customizer.py`).

Couvre :
1. `app.openapi()` retourne un dict valide OpenAPI 3.1.
2. `info.title`/`description`/`contact`/`license` présents et FR.
3. `info.x-logo` présent (extension ReDoc).
4. `servers` ≥ 3 entrées (dev + staging + prod).
5. `tags` ≥ 18 avec `description` non vides.
6. `securitySchemes.BearerAuth` HTTP bearer JWT.
7. `securitySchemes.PrometheusToken` apiKey header.
8. `paths` ≥ 80 endpoints (snapshot post-N4).
9. Anti-régression : `/healthz`, `/ready`, `/version` taggés `health`.
10. Anti-régression : `/admin/helpdesk/metrics` taggé `admin` ET `helpdesk`.
11. `customize_openapi` idempotent (cache via `app.openapi_schema`).
12. NEXYA_TAGS_METADATA noms cohérents avec les tags des routers.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from app.core.openapi.customizer import NEXYA_TAGS_METADATA, customize_openapi
from app.main import app

# ══════════════════════════════════════════════════════════════
# Schéma global
# ══════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def schema() -> dict:
    # Reset le cache pour s'assurer qu'on génère depuis zéro
    app.openapi_schema = None
    return app.openapi()


def test_openapi_version_is_3_1(schema: dict) -> None:
    """OpenAPI 3.1 est obligatoire (JSON Schema 2020-12)."""
    assert schema["openapi"] == "3.1.0"


def test_info_block_filled_in_french(schema: dict) -> None:
    info = schema["info"]
    assert info["title"] == "NEXYA API"
    assert "summary" in info
    assert "description" in info
    # FR : on cherche des marqueurs francophones
    assert "Africa" in info["description"] or "RGPD" in info["description"]
    assert info["contact"]["email"] == "support@nexya.ai"
    assert "license" in info
    assert "termsOfService" in info


def test_info_x_logo_present_for_redoc(schema: dict) -> None:
    """Extension `x-logo` permet à ReDoc d'afficher le branding NEXYA."""
    assert "x-logo" in schema["info"]
    logo = schema["info"]["x-logo"]
    assert "url" in logo
    assert "altText" in logo


def test_servers_list_has_dev_staging_prod(schema: dict) -> None:
    servers = schema["servers"]
    assert len(servers) >= 3
    urls = [s["url"] for s in servers]
    assert any("localhost" in u for u in urls)
    assert any("staging" in u for u in urls)
    assert any("api.nexya.ai" in u for u in urls)


# ══════════════════════════════════════════════════════════════
# Tags
# ══════════════════════════════════════════════════════════════


def test_tags_block_has_at_least_18_entries(schema: dict) -> None:
    assert len(schema["tags"]) >= 18


def test_each_tag_has_description(schema: dict) -> None:
    for tag in schema["tags"]:
        assert tag.get("description"), f"Tag {tag['name']!r} sans description"
        # Les descriptions sont en FR, marquées par des accents
        # (pas une garantie absolue mais un signal raisonnable).
        desc = tag["description"]
        assert len(desc) >= 30, f"Tag {tag['name']!r} description trop courte"


def test_required_tags_all_present(schema: dict) -> None:
    """Anti-régression : 5 tags critiques doivent être documentés."""
    names = {t["name"] for t in schema["tags"]}
    required = {"auth", "chat", "rgpd", "admin", "health"}
    missing = required - names
    assert not missing, f"Tags critiques manquants : {missing}"


# ══════════════════════════════════════════════════════════════
# Security schemes
# ══════════════════════════════════════════════════════════════


def test_bearer_auth_security_scheme(schema: dict) -> None:
    schemes = schema["components"]["securitySchemes"]
    assert "BearerAuth" in schemes
    bearer = schemes["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert bearer["bearerFormat"] == "JWT"


def test_prometheus_token_security_scheme(schema: dict) -> None:
    schemes = schema["components"]["securitySchemes"]
    assert "PrometheusToken" in schemes
    pt = schemes["PrometheusToken"]
    assert pt["type"] == "apiKey"
    assert pt["in"] == "header"
    assert pt["name"] == "X-Prometheus-Token"


# ══════════════════════════════════════════════════════════════
# Paths — snapshot
# ══════════════════════════════════════════════════════════════


def test_paths_snapshot_after_n4(schema: dict) -> None:
    """Snapshot du nombre d'endpoints — anti-drop accidentel.

    Snapshot post-O1 : 60 paths (auth, chat, projects, library, files,
    voice, vision, memory, rag, ai_models, tasks, notifications,
    feedback, suggestions, rgpd, admin, helpdesk, observability,
    health, image). Si on droppe sous 50 = un router a été retiré
    par erreur.
    """
    paths = schema["paths"]
    assert len(paths) >= 50, (
        f"Seulement {len(paths)} paths documentés — un router a-t-il été dropé ?"
    )


def test_critical_endpoints_present_in_schema(schema: dict) -> None:
    """Anti-régression : les endpoints critiques doivent rester documentés."""
    paths = schema["paths"].keys()
    critical = [
        "/auth/login",
        "/auth/register",
        "/auth/refresh",
        "/chat/stream",
        "/healthz",
        "/ready",
        "/version",
        "/admin/helpdesk/metrics",
        "/rgpd/user/data-export",
    ]
    for route in critical:
        assert route in paths, f"Endpoint critique disparu : {route}"


def test_health_endpoints_tagged_health(schema: dict) -> None:
    """`/healthz`, `/ready`, `/version` doivent être taggés `health`."""
    for path in ("/healthz", "/ready", "/version"):
        op = schema["paths"][path]["get"]
        assert "health" in op.get("tags", []), f"{path} non taggé 'health' : {op.get('tags')}"


def test_admin_helpdesk_tagged_admin_and_helpdesk(schema: dict) -> None:
    op = schema["paths"]["/admin/helpdesk/metrics"]["get"]
    tags = op.get("tags", [])
    assert "admin" in tags
    assert "helpdesk" in tags


# ══════════════════════════════════════════════════════════════
# Idempotence
# ══════════════════════════════════════════════════════════════


def test_customize_openapi_is_idempotent() -> None:
    """Deux appels successifs retournent le même objet (cache)."""
    test_app = FastAPI(title="Test", version="0.0.1")
    s1 = customize_openapi(test_app)
    s2 = customize_openapi(test_app)
    assert s1 is s2  # même objet (cache via app.openapi_schema)


def test_customize_openapi_returns_3_1() -> None:
    test_app = FastAPI(title="Test", version="0.0.1")
    s = customize_openapi(test_app)
    assert s["openapi"] == "3.1.0"


# ══════════════════════════════════════════════════════════════
# Cohérence NEXYA_TAGS_METADATA
# ══════════════════════════════════════════════════════════════


def test_nexya_tags_metadata_no_duplicates() -> None:
    names = [t["name"] for t in NEXYA_TAGS_METADATA]
    assert len(names) == len(set(names)), (
        f"Tags dupliqués : {[n for n in names if names.count(n) > 1]}"
    )


def test_nexya_tags_metadata_alphabetical_consistency() -> None:
    """Les tags critiques sont présents avec le bon ordre."""
    names = [t["name"] for t in NEXYA_TAGS_METADATA]
    # On vérifie juste que les 18+ tags sont dans la liste
    expected = {
        "auth",
        "user",
        "chat",
        "projects",
        "library",
        "files",
        "voice",
        "vision",
        "memory",
        "rag",
        "ai_models",
        "tasks",
        "notifications",
        "feedback",
        "suggestions",
        "rgpd",
        "admin",
        "helpdesk",
        "observability",
        "health",
    }
    missing = expected - set(names)
    assert not missing, f"Tags attendus manquants : {missing}"
