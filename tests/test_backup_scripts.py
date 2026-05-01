"""Tests structurels des scripts backup_db.sh + restore_db.sh.

Pattern aligné sur tests/test_rollback_script.py + tests/test_smoke_test_script.py.
Ne lance PAS pg_dump réel — vérifie uniquement la structure du script
(strict bash, dry-run présent, args parsing, env vars utilisées).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup_db.sh"
RESTORE = ROOT / "scripts" / "restore_db.sh"

# Résout le chemin absolu de bash (Git Bash sur Windows, /usr/bin/bash sur Linux).
# Skip gracieux si bash absent.
bash = shutil.which("bash")
requires_bash = pytest.mark.skipif(
    bash is None, reason="bash required (Git Bash sur Windows)"
)


# ══════════════════════════════════════════════════════════════
# backup_db.sh
# ══════════════════════════════════════════════════════════════


def test_backup_script_exists() -> None:
    assert BACKUP.exists(), "scripts/backup_db.sh doit exister"


def test_backup_script_strict_bash() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content


@requires_bash
def test_backup_script_syntax_check() -> None:
    """`bash -n` vérifie la syntaxe sans exécuter le script."""
    result = subprocess.run(
        [bash, "-n", str(BACKUP)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_backup_script_has_dry_run_flag() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "--dry-run" in content
    assert "DRY_RUN" in content


def test_backup_script_has_help_flag() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "--help" in content or "-h)" in content


def test_backup_script_uses_env_vars() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    for var in (
        "BACKUP_DIR",
        "S3_BUCKET",
        "POSTGRES_CONTAINER",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "BACKUP_RETENTION_DAYS",
    ):
        assert var in content, f"env var {var} doit être lue"


def test_backup_script_uses_pg_dump_with_custom_format() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "pg_dump" in content
    assert "--format=custom" in content
    assert "--compress=9" in content


def test_backup_script_uses_sse_aes256() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "--sse AES256" in content


def test_backup_script_has_lock() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "flock" in content


def test_backup_script_supports_gpg_encryption() -> None:
    content = BACKUP.read_text(encoding="utf-8")
    assert "BACKUP_GPG_RECIPIENT" in content
    assert "gpg" in content


def test_backup_script_documents_exit_codes() -> None:
    """Le header doit documenter les exit codes."""
    content = BACKUP.read_text(encoding="utf-8")
    assert "Exit codes" in content


@requires_bash
def test_backup_script_dry_run_does_not_call_pg_dump() -> None:
    """En --dry-run, le script ne doit pas appeler pg_dump réellement."""
    result = subprocess.run(
        [bash, str(BACKUP), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # Exit 0 (deps OK) ou 3 (deps manquantes en CI/Windows) acceptés
    assert result.returncode in (0, 3), (
        f"Exit code inattendu={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Si le script a démarré, il doit afficher [DRY-RUN] quelque part
    if result.returncode == 0:
        combined = result.stdout + result.stderr
        assert "[DRY-RUN]" in combined or "DRY-RUN" in combined


# ══════════════════════════════════════════════════════════════
# restore_db.sh
# ══════════════════════════════════════════════════════════════


def test_restore_script_exists() -> None:
    assert RESTORE.exists()


def test_restore_script_strict_bash() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content


@requires_bash
def test_restore_script_syntax_check() -> None:
    result = subprocess.run(
        [bash, "-n", str(RESTORE)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_restore_script_validates_post_restore_integrity() -> None:
    """Le script doit faire les vérifs post-restore (count users + alembic + FK)."""
    content = RESTORE.read_text(encoding="utf-8")
    assert "count" in content.lower()
    assert "users" in content
    assert "alembic_version" in content


def test_restore_script_supports_swap() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "--swap" in content
    assert "ALTER DATABASE" in content


def test_restore_script_supports_dry_run() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "--dry-run" in content


def test_restore_script_requires_s3_path_arg() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "s3://" in content


def test_restore_script_supports_target_db_override() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "--target-db" in content
    assert "TARGET_DB" in content


def test_restore_script_uses_pg_restore() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "pg_restore" in content


def test_restore_script_verifies_sha256() -> None:
    content = RESTORE.read_text(encoding="utf-8")
    assert "sha256sum" in content


def test_restore_script_swap_terminates_active_connections() -> None:
    """Avant ALTER DATABASE RENAME, il faut tuer les connexions actives."""
    content = RESTORE.read_text(encoding="utf-8")
    assert "pg_terminate_backend" in content


@requires_bash
def test_restore_script_no_args_returns_usage_error() -> None:
    """Sans argument, le script doit retourner un code d'erreur (usage)."""
    result = subprocess.run(
        [bash, str(RESTORE)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2, (
        f"Sans arg, exit code attendu=2 (args fail), reçu={result.returncode}"
    )


@requires_bash
def test_restore_script_invalid_path_rejected() -> None:
    """Un chemin S3 mal formé doit être rejeté avec exit 2."""
    result = subprocess.run(
        [bash, str(RESTORE), "/local/path.dump"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
