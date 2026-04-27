"""
Tests O2 — validation structure des exports DD-ready.

Couvre :
1. `docs/api/openapi.json` existe + JSON valide + `openapi == "3.1.0"`
2. `openapi.json` contient ≥ 50 paths + ≥ 18 tags
3. `README.md` racine existe + section « Onboarding 5 minutes »
4. `docs/glossary.md` ≥ 30 termes
5. `docs/architecture/` contient les 7 fichiers attendus
6. `docs/compliance/` contient les 4 fichiers
7. `docs/api/` contient les 3 fichiers + openapi.json
8. `docs/runbooks/` contient les 3 fichiers
9. `docs/adr/` contient les 5 ADRs
10. Cross-check : 19 migrations Alembic ↔ schema.sql contient `CREATE TABLE`

Note : `schema.sql` n'est pas vérifié strictement (généré uniquement
quand `pg_dump` + DB disponibles). On teste son existence si présent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
ARCH_DIR = DOCS / "architecture"
COMP_DIR = DOCS / "compliance"
API_DIR = DOCS / "api"
RUNBOOKS_DIR = DOCS / "runbooks"
ADR_DIR = DOCS / "adr"
README = ROOT / "README.md"
GLOSSARY = DOCS / "glossary.md"
OPENAPI_JSON = API_DIR / "openapi.json"
SCHEMA_SQL = ARCH_DIR / "schema.sql"
MIGRATIONS_DIR = ROOT / "migrations" / "versions"


# ══════════════════════════════════════════════════════════════
# README racine
# ══════════════════════════════════════════════════════════════


def test_root_readme_exists() -> None:
    assert README.exists(), "README.md racine manquant — DD blocker"


def test_root_readme_has_onboarding_section() -> None:
    content = README.read_text(encoding="utf-8")
    assert "Onboarding 5 minutes" in content, (
        "README sans section 'Onboarding 5 minutes' — DD blocker"
    )
    # Sanity : commandes critiques mentionnées
    assert "alembic upgrade head" in content
    assert "uvicorn app.main:app" in content


def test_root_readme_has_dd_documentation_pointer() -> None:
    content = README.read_text(encoding="utf-8")
    assert "Due Diligence" in content
    assert "docs/architecture/" in content
    assert "docs/compliance/" in content


# ══════════════════════════════════════════════════════════════
# Glossary
# ══════════════════════════════════════════════════════════════


def test_glossary_exists_and_has_30_plus_terms() -> None:
    assert GLOSSARY.exists(), "docs/glossary.md manquant"
    content = GLOSSARY.read_text(encoding="utf-8")
    # Compter les bullet items (lignes commençant par `- **<terme>**`)
    bullets = [l for l in content.splitlines() if l.lstrip().startswith("- **")]
    assert len(bullets) >= 30, (
        f"Glossary ne contient que {len(bullets)} termes (cible 30+)"
    )


# ══════════════════════════════════════════════════════════════
# Architecture docs (7 fichiers)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "filename",
    [
        "overview.md",
        "data-model.md",
        "request-flow.md",
        "ai-architecture.md",
        "security-posture.md",
        "observability.md",
        "payments-readiness.md",
    ],
)
def test_architecture_doc_exists(filename: str) -> None:
    path = ARCH_DIR / filename
    assert path.exists(), f"docs/architecture/{filename} manquant"
    content = path.read_text(encoding="utf-8")
    assert len(content) > 500, (
        f"docs/architecture/{filename} trop court (< 500 chars)"
    )


def test_architecture_overview_has_mermaid_diagram() -> None:
    content = (ARCH_DIR / "overview.md").read_text(encoding="utf-8")
    assert "```mermaid" in content


# ══════════════════════════════════════════════════════════════
# Compliance docs (4 fichiers)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "filename",
    [
        "rgpd.md",
        "ai-act.md",
        "security-checklist.md",
        "dpa-template.md",
    ],
)
def test_compliance_doc_exists(filename: str) -> None:
    path = COMP_DIR / filename
    assert path.exists(), f"docs/compliance/{filename} manquant"


def test_rgpd_doc_mentions_articles() -> None:
    content = (COMP_DIR / "rgpd.md").read_text(encoding="utf-8")
    for article in ("Article 7", "Article 15", "Article 17", "Article 20"):
        assert article in content, f"docs/compliance/rgpd.md sans {article}"


def test_security_checklist_mentions_owasp_top_10() -> None:
    content = (COMP_DIR / "security-checklist.md").read_text(encoding="utf-8")
    # Au moins 8 catégories OWASP A0X mentionnées
    matches = sum(1 for i in range(1, 11) if f"A0{i}:2021" in content or f"A1{i-10}:2021" in content)
    assert matches >= 8, f"OWASP Top 10 incomplet ({matches}/10)"


# ══════════════════════════════════════════════════════════════
# API docs (3 fichiers + openapi.json)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "filename",
    ["endpoints.md", "error-codes.md", "versioning.md"],
)
def test_api_doc_exists(filename: str) -> None:
    path = API_DIR / filename
    assert path.exists(), f"docs/api/{filename} manquant"


def test_openapi_json_exists_and_valid() -> None:
    assert OPENAPI_JSON.exists(), (
        "docs/api/openapi.json manquant — runner "
        "'python -m scripts.export_openapi'"
    )
    data = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("openapi") == "3.1.0"


def test_openapi_json_has_50_plus_paths() -> None:
    data = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    paths = data.get("paths", {})
    assert len(paths) >= 50, (
        f"openapi.json contient seulement {len(paths)} paths (cible ≥ 50)"
    )


def test_openapi_json_has_18_plus_tags() -> None:
    data = json.loads(OPENAPI_JSON.read_text(encoding="utf-8"))
    tags = data.get("tags", [])
    assert len(tags) >= 18, (
        f"openapi.json contient seulement {len(tags)} tags (cible ≥ 18)"
    )


def test_error_codes_mentions_critical_codes() -> None:
    content = (API_DIR / "error-codes.md").read_text(encoding="utf-8")
    critical = [
        "AUTH_TOKEN_EXPIRED",
        "RATE_LIMIT_EXCEEDED",
        "LLM_UNAVAILABLE",
        "VALIDATION_ERROR",
        "RESOURCE_NOT_FOUND",
        "PLAN_REQUIRED",
    ]
    for code in critical:
        assert code in content, f"docs/api/error-codes.md sans `{code}`"


# ══════════════════════════════════════════════════════════════
# Runbooks (3 fichiers)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "filename",
    [
        "incident-response.md",
        "deployment-l2.md",
        "db-restore.md",
    ],
)
def test_runbook_exists(filename: str) -> None:
    path = RUNBOOKS_DIR / filename
    assert path.exists(), f"docs/runbooks/{filename} manquant"


# ══════════════════════════════════════════════════════════════
# ADRs (5 fichiers)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "filename",
    [
        "0001-fastapi-vs-django.md",
        "0002-sqlalchemy-async.md",
        "0003-redis-rate-limiting.md",
        "0004-jwt-rs256-vs-hs256.md",
        "0005-llm-router-mock-first.md",
    ],
)
def test_adr_exists(filename: str) -> None:
    path = ADR_DIR / filename
    assert path.exists(), f"docs/adr/{filename} manquant"


@pytest.mark.parametrize(
    "filename",
    [
        "0001-fastapi-vs-django.md",
        "0002-sqlalchemy-async.md",
        "0003-redis-rate-limiting.md",
        "0004-jwt-rs256-vs-hs256.md",
        "0005-llm-router-mock-first.md",
    ],
)
def test_adr_follows_nygard_format(filename: str) -> None:
    """Format Nygard : Status / Context / Decision / Consequences."""
    content = (ADR_DIR / filename).read_text(encoding="utf-8")
    for section in ("## Status", "## Context", "## Decision", "## Consequences"):
        assert section in content, (
            f"{filename} ne suit pas le format Nygard (section {section} manquante)"
        )


# ══════════════════════════════════════════════════════════════
# Cross-check migrations ↔ schema.sql
# ══════════════════════════════════════════════════════════════


def test_migrations_count_19_plus() -> None:
    """Anti-régression : on a 19+ migrations livrées."""
    migrations = sorted(MIGRATIONS_DIR.glob("*.py"))
    # exclure __init__.py / __pycache__
    real_migrations = [m for m in migrations if not m.name.startswith("_")]
    assert len(real_migrations) >= 19, (
        f"Seulement {len(real_migrations)} migrations trouvées (cible ≥ 19)"
    )


def test_schema_sql_exists_or_skip() -> None:
    """schema.sql est généré par `bash scripts/export_schema.sh` — on
    skip si absent (cas dev sans Postgres local au moment du test)."""
    if not SCHEMA_SQL.exists():
        pytest.skip("docs/architecture/schema.sql absent — runner 'bash scripts/export_schema.sh'")
    content = SCHEMA_SQL.read_text(encoding="utf-8")
    assert "CREATE TABLE" in content
    # Sanity : au moins 19 tables
    create_tables = content.count("CREATE TABLE")
    assert create_tables >= 19, (
        f"schema.sql contient seulement {create_tables} CREATE TABLE (cible ≥ 19)"
    )


# ══════════════════════════════════════════════════════════════
# Scripts d'export
# ══════════════════════════════════════════════════════════════


def test_export_openapi_script_exists() -> None:
    path = ROOT / "scripts" / "export_openapi.py"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "from app.main import app" in content
    assert "app.openapi()" in content


def test_export_schema_script_exists() -> None:
    path = ROOT / "scripts" / "export_schema.sh"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content
    assert "pg_dump" in content
    assert "--schema-only" in content


# ══════════════════════════════════════════════════════════════
# Workflow GHA freshness
# ══════════════════════════════════════════════════════════════


def test_dd_exports_workflow_exists() -> None:
    workflow = ROOT / ".github" / "workflows" / "dd-exports-fresh.yml"
    assert workflow.exists()
    content = workflow.read_text(encoding="utf-8")
    assert "git diff --exit-code" in content
    assert "export_openapi" in content
    assert "export_schema" in content
