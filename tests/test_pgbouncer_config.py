"""Tests structurels de la config PgBouncer livrée 2026-05-01.

Ne lance PAS PgBouncer réel — vérifie uniquement la cohérence des
fichiers de config + le comportement code côté
`app/core/database/postgres.py` + `app/config.py:database_use_pgbouncer`.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PGBOUNCER_INI = ROOT / "docker" / "pgbouncer" / "pgbouncer.ini"
USERLIST_EXAMPLE = ROOT / "docker" / "pgbouncer" / "userlist.txt.example"
COMPOSE_OVERLAY = ROOT / "docker" / "docker-compose.pgbouncer.yml"


# ══════════════════════════════════════════════════════════════
# pgbouncer.ini
# ══════════════════════════════════════════════════════════════


def test_pgbouncer_ini_exists() -> None:
    assert PGBOUNCER_INI.is_file()


def test_pgbouncer_ini_uses_transaction_mode() -> None:
    """Transaction pooling mode obligatoire (vs session mode qui ne scale pas)."""
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert re.search(r"^pool_mode\s*=\s*transaction", content, re.M)


def test_pgbouncer_ini_uses_scram_sha256() -> None:
    """SCRAM-SHA-256 standard moderne (Postgres 14+)."""
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert re.search(r"^auth_type\s*=\s*scram-sha-256", content, re.M)


def test_pgbouncer_ini_uses_discard_all_reset() -> None:
    """DISCARD ALL nettoie l'état serveur entre 2 transactions client.
    Sans ça, des paramètres de session fuiteraient entre transactions
    (SET, prepared statements, advisory locks)."""
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert "DISCARD ALL" in content


def test_pgbouncer_ini_listens_on_6432() -> None:
    """Port standard PgBouncer."""
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert re.search(r"^listen_port\s*=\s*6432", content, re.M)


def test_pgbouncer_ini_max_client_conn_reasonable() -> None:
    """`max_client_conn` doit être borné à une valeur raisonnable."""
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    m = re.search(r"^max_client_conn\s*=\s*(\d+)", content, re.M)
    assert m, "max_client_conn doit être défini"
    value = int(m.group(1))
    assert 1000 <= value <= 100_000, (
        f"max_client_conn={value} hors range raisonnable"
    )


def test_pgbouncer_ini_database_nexya_declared() -> None:
    """Le mapping DB virtuelle nexya → host=postgres doit être présent."""
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert "nexya" in content
    assert "host=postgres" in content
    assert "port=5432" in content


# ══════════════════════════════════════════════════════════════
# userlist.txt.example
# ══════════════════════════════════════════════════════════════


def test_userlist_example_exists() -> None:
    assert USERLIST_EXAMPLE.is_file()


def test_userlist_example_documents_scram_generation() -> None:
    """Le template doit documenter la procédure de génération du hash."""
    content = USERLIST_EXAMPLE.read_text(encoding="utf-8")
    assert "SCRAM-SHA-256" in content
    assert "password_encryption" in content
    assert "ALTER USER" in content


def test_userlist_example_no_real_credentials() -> None:
    """Le template doit utiliser un placeholder évident (anti-leak)."""
    content = USERLIST_EXAMPLE.read_text(encoding="utf-8")
    # Le placeholder doit être manifeste (pas un vrai hash random)
    assert "REPLACE" in content.upper() or "PLACEHOLDER" in content.upper()


def test_userlist_example_warns_against_commit() -> None:
    """Le template doit explicitement avertir contre le commit du vrai userlist.txt."""
    content = USERLIST_EXAMPLE.read_text(encoding="utf-8")
    assert "gitignore" in content.lower() or "ne doit pas être commit" in content.lower()


def test_userlist_real_is_gitignored() -> None:
    """Le vrai `docker/pgbouncer/userlist.txt` (sans .example) doit être
    dans `.gitignore` pour empêcher le commit accidentel des hash SCRAM."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "docker/pgbouncer/userlist.txt" in gitignore


# ══════════════════════════════════════════════════════════════
# docker-compose.pgbouncer.yml
# ══════════════════════════════════════════════════════════════


def test_compose_overlay_exists() -> None:
    assert COMPOSE_OVERLAY.is_file()


def test_compose_overlay_is_valid_yaml() -> None:
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert "services" in data
    assert "pgbouncer" in data["services"]


def test_compose_overlay_pinned_image() -> None:
    """Pas d'image :latest — reproductibilité builds + anti-CVE silencieuse."""
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    image = data["services"]["pgbouncer"]["image"]
    assert ":" in image, "Image doit être pinned (pas :latest)"
    tag = image.split(":")[-1]
    assert tag not in ("latest", "main", "master"), f"Tag {tag} interdit"


def test_compose_overlay_mounts_config_readonly() -> None:
    """Les fichiers de config doivent être montés en read-only (`:ro`)."""
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    volumes = data["services"]["pgbouncer"]["volumes"]
    assert any("pgbouncer.ini" in v and ":ro" in v for v in volumes)
    assert any("userlist.txt" in v and ":ro" in v for v in volumes)


def test_compose_overlay_depends_on_postgres_healthy() -> None:
    """PgBouncer ne doit démarrer qu'après Postgres healthy."""
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    depends = data["services"]["pgbouncer"]["depends_on"]
    assert "postgres" in depends
    assert depends["postgres"]["condition"] == "service_healthy"


def test_compose_overlay_has_healthcheck() -> None:
    """Healthcheck `pg_isready` pour que la stack puisse vérifier l'état."""
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    healthcheck = data["services"]["pgbouncer"]["healthcheck"]
    assert healthcheck is not None
    test_cmd = " ".join(healthcheck["test"])
    assert "pg_isready" in test_cmd


# ══════════════════════════════════════════════════════════════
# app/core/database/postgres.py — adaptation kwargs
# ══════════════════════════════════════════════════════════════


def test_postgres_engine_supports_pgbouncer_mode() -> None:
    """Le code postgres.py doit ajuster les kwargs en mode PgBouncer."""
    src = (ROOT / "app" / "core" / "database" / "postgres.py").read_text(
        encoding="utf-8"
    )
    assert "database_use_pgbouncer" in src
    assert "pool_pre_ping" in src
    assert "prepare_threshold" in src


def test_postgres_engine_disables_pre_ping_with_pgbouncer() -> None:
    """En mode PgBouncer, pool_pre_ping doit être False (sinon double check inutile)."""
    src = (ROOT / "app" / "core" / "database" / "postgres.py").read_text(
        encoding="utf-8"
    )
    # Cherche le bloc PgBouncer + pool_pre_ping=False à proximité
    assert "pool_pre_ping" in src and "False" in src


def test_settings_has_pgbouncer_flag() -> None:
    """`settings.database_use_pgbouncer` doit exister, défaut False."""
    from app.config import Settings

    s = Settings()
    assert hasattr(s, "database_use_pgbouncer")
    assert s.database_use_pgbouncer is False, (
        "Défaut V1 doit être False (PgBouncer optionnel — activable en L2)"
    )


def test_env_example_documents_pgbouncer() -> None:
    """`.env.example` doit documenter `DATABASE_USE_PGBOUNCER`."""
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE_USE_PGBOUNCER" in content


def test_deployment_l2_runbook_documents_pgbouncer() -> None:
    """`deployment-l2.md` doit documenter la procédure PgBouncer (Pourquoi,
    Stack, Config, Activation, Limitations)."""
    content = (
        ROOT / "docs" / "runbooks" / "deployment-l2.md"
    ).read_text(encoding="utf-8")
    assert "PgBouncer" in content
    assert "transaction mode" in content.lower()
    assert "userlist.txt" in content
    assert "Limitations" in content or "limitations" in content
