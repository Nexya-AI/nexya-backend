"""L1 — Validation structure scripts/rollback.sh.

Tranche 6 (2026-04-26) : skipif gracieux si bash absent (Windows
sans Git Bash).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROLLBACK_SH = Path(__file__).resolve().parents[1] / "scripts" / "rollback.sh"

bash = shutil.which("bash")
requires_bash = pytest.mark.skipif(bash is None, reason="bash required (Git Bash sur Windows)")


def _read() -> str:
    return ROLLBACK_SH.read_text(encoding="utf-8")


def test_rollback_script_exists():
    assert ROLLBACK_SH.is_file()


def test_rollback_has_strict_bash_mode():
    """`set -euo pipefail` : crucial pour un rollback prod (un cd /tmp
    qui plante DOIT arrêter le script, pas continuer en silence)."""
    content = _read()
    assert "set -euo pipefail" in content


def test_rollback_validates_tag_format():
    """Le script doit refuser un tag mal formaté avant tout pull Docker."""
    content = _read()
    # Cherche une regex semver
    assert "TAG_REGEX" in content
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in content


def test_rollback_supports_dry_run():
    content = _read()
    assert "DRY_RUN" in content
    assert "--dry-run" in content


@requires_bash
def test_rollback_bash_syntax_check():
    """`bash -n` valide la syntaxe sans exécuter."""
    result = subprocess.run(
        [bash, "-n", str(ROLLBACK_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash syntax error:\n{result.stderr}"


@requires_bash
def test_rollback_dry_run_prints_commands():
    """En --dry-run, le script affiche les commandes Docker prévues
    sans les exécuter."""
    result = subprocess.run(
        [bash, str(ROLLBACK_SH), "--dry-run", "v1.2.3"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    # L'output doit mentionner des commandes Docker (pull, compose).
    assert "[DRY-RUN]" in output, "dry-run mode silencieux"
    assert "docker pull" in output


@requires_bash
def test_rollback_refuses_invalid_tag():
    """Un tag malformé doit faire exit 1."""
    result = subprocess.run(
        [bash, str(ROLLBACK_SH), "foo-bar"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0


def test_rollback_calls_smoke_test():
    """Le rollback doit appeler smoke_test.sh post-restart."""
    content = _read()
    assert "smoke_test.sh" in content


def test_rollback_has_trap_for_interruption():
    """Trap INT/TERM pour log proprement si Ctrl+C en plein rollback."""
    content = _read()
    assert "trap" in content


def test_rollback_uses_compose_down_with_timeout():
    """`docker compose down --timeout 30` pour un graceful shutdown.
    Sans timeout, les conteneurs sont SIGKILL après 10s par défaut,
    risque de corrompre les données en cours d'écriture."""
    content = _read()
    assert "--timeout 30" in content or "--timeout=30" in content
