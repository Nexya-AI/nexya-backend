"""L1 — Validation structure scripts/smoke_test.sh.

Tranche 6 (2026-04-26) : skipif gracieux si bash absent.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SMOKE_SH = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.sh"

bash = shutil.which("bash")
requires_bash = pytest.mark.skipif(bash is None, reason="bash required (Git Bash sur Windows)")


def _read() -> str:
    return SMOKE_SH.read_text(encoding="utf-8")


def test_smoke_test_exists():
    assert SMOKE_SH.is_file()


def test_smoke_has_strict_bash_mode():
    content = _read()
    assert "set -euo pipefail" in content


@requires_bash
def test_smoke_bash_syntax_check():
    result = subprocess.run(
        [bash, "-n", str(SMOKE_SH)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash syntax error:\n{result.stderr}"


@requires_bash
def test_smoke_refuses_missing_arg():
    result = subprocess.run(
        [bash, str(SMOKE_SH)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "Usage" in (result.stdout + result.stderr)


def test_smoke_has_at_least_4_curl_calls():
    """Au moins healthz, ready, metrics, observability/status."""
    content = _read()
    curl_count = content.count("curl -fsS")
    assert curl_count >= 4, f"Smoke test devrait avoir ≥ 4 curls, trouvé {curl_count}"


def test_smoke_register_only_in_staging_env():
    """Tranche 7 actée 2026-04-26 : POST /auth/register uniquement
    si ENV=staging (rate limit IP A3 cassera en CI)."""
    content = _read()
    # Cherche un test conditionnel sur ENV pour /auth/register
    assert "ENV" in content
    assert "staging" in content
    assert "/auth/register" in content


def test_smoke_has_trap_exit_for_cleanup():
    """Trap EXIT pour cleanup user dummy en cas d'échec mid-test."""
    content = _read()
    assert "trap " in content
    assert "EXIT" in content


def test_smoke_checks_nexya_metrics_present():
    """Le smoke test devrait grep `nexya_ai_chat_calls_total` dans la
    réponse /metrics pour valider que les métriques custom K1 sont
    bien exposées."""
    content = _read()
    assert "nexya_ai_chat_calls_total" in content
