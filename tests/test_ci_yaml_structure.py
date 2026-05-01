"""L1 — Validation structure des workflows GitHub Actions YAML.

Vérifie pour chaque workflow :
- YAML valide
- Jobs attendus présents
- Concurrency cancel-in-progress activé
- Permissions least-privilege
- Versions actions pinned (pas @main, pas @latest)
- Whitelist organisations connues
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

# Tranche 9 actée 2026-04-26 : ajout de `dependabot/` à la whitelist.
ALLOWED_ORGS = {
    "actions",
    "docker",
    "astral-sh",
    "softprops",
    "github",
    "dependabot",
    # N4 — k6 load tests (org Grafana Labs, action officielle)
    "grafana",
}

# Pattern action use : `org/action@ref`
ACTION_REGEX = re.compile(r"^([a-zA-Z0-9_-]+)/([a-zA-Z0-9_/-]+)@([\w.-]+)$")


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _all_workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml"))


def _extract_action_uses(workflow: dict) -> list[str]:
    """Extrait toutes les valeurs `uses:` du workflow (par job + par step)."""
    uses_list: list[str] = []
    jobs = workflow.get("jobs", {}) or {}
    for job in jobs.values():
        # Job-level `uses:` (réutilisation de workflow)
        if isinstance(job, dict) and "uses" in job:
            uses_list.append(job["uses"])
        # Step-level `uses:`
        for step in job.get("steps", []) or []:
            if isinstance(step, dict) and "uses" in step:
                uses_list.append(step["uses"])
    return uses_list


def test_workflows_dir_exists():
    assert WORKFLOWS_DIR.is_dir(), f"Missing workflows dir: {WORKFLOWS_DIR}"


def test_four_workflows_present():
    """Workflows GHA livrés (L1=4 + N3 evals + N4 load + O2 dd-exports = 7)."""
    files = _all_workflow_files()
    names = {p.name for p in files}
    expected = {
        "ci.yml",
        "release.yml",
        "codeql.yml",
        "dependabot-auto-merge.yml",
        # N3 — évals IA reproductibles (cron nightly + PR comment)
        "evals.yml",
        # N4 — tests de charge k6 (workflow_dispatch + cron weekly)
        "load.yml",
        # O2 — DD exports freshness (push main)
        "dd-exports-fresh.yml",
    }
    assert names == expected, f"Workflow files mismatch: {names} vs {expected}"


@pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
def test_workflow_yaml_valid(path: Path):
    data = _load_yaml(path)
    assert isinstance(data, dict)
    assert "jobs" in data
    # En YAML, `on:` est parfois parsé comme `True` (mot-clé booléen).
    # On accepte les deux formes pour ne pas planter.
    assert "on" in data or True in data, f"{path.name} missing 'on' trigger"


def test_ci_yml_has_six_jobs():
    data = _load_yaml(WORKFLOWS_DIR / "ci.yml")
    expected_jobs = {
        "lint",
        "typecheck",
        "security-scan",
        "tests",
        "docker-build",
        "migrations-check",
    }
    actual_jobs = set(data["jobs"].keys())
    assert actual_jobs == expected_jobs, f"ci.yml jobs mismatch: {actual_jobs} vs {expected_jobs}"


def test_ci_yml_has_workflow_call_trigger():
    """Tranche 2 actée 2026-04-26 : ci.yml doit déclarer workflow_call
    pour que release.yml puisse le réutiliser."""
    data = _load_yaml(WORKFLOWS_DIR / "ci.yml")
    triggers = data.get("on") or data.get(True)
    assert isinstance(triggers, dict)
    assert "workflow_call" in triggers


def test_release_yml_has_four_jobs():
    data = _load_yaml(WORKFLOWS_DIR / "release.yml")
    expected_jobs = {
        "validate",
        "build-and-push",
        "create-github-release",
        "notify",
    }
    actual_jobs = set(data["jobs"].keys())
    assert actual_jobs == expected_jobs


def test_release_yml_validate_uses_ci_workflow_call():
    data = _load_yaml(WORKFLOWS_DIR / "release.yml")
    validate = data["jobs"]["validate"]
    assert validate.get("uses", "").endswith("ci.yml")


def test_codeql_yml_has_analyze_job():
    data = _load_yaml(WORKFLOWS_DIR / "codeql.yml")
    assert "analyze" in data["jobs"]


def test_dependabot_auto_merge_filters_dependabot_actor():
    data = _load_yaml(WORKFLOWS_DIR / "dependabot-auto-merge.yml")
    job = data["jobs"]["auto-merge"]
    assert "dependabot[bot]" in str(job.get("if", ""))


@pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
def test_workflow_has_concurrency_cancel_in_progress(path: Path):
    data = _load_yaml(path)
    concurrency = data.get("concurrency")
    assert concurrency is not None, f"{path.name} missing concurrency"
    # Sur ci.yml, codeql.yml, dependabot-auto-merge.yml → cancel-in-progress: true
    # Sur release.yml → cancel-in-progress: false (intentionnel — un tag
    # ne s'annule pas)
    assert "cancel-in-progress" in concurrency


@pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
def test_workflow_has_permissions_block(path: Path):
    """Principle of least privilege : chaque workflow doit déclarer
    explicitement ses permissions (jamais write-all par défaut)."""
    data = _load_yaml(path)
    # Soit au top-level, soit dans chaque job — au moins l'un des deux.
    has_top_level = "permissions" in data
    has_job_level = any(
        "permissions" in job for job in data.get("jobs", {}).values() if isinstance(job, dict)
    )
    assert has_top_level or has_job_level, (
        f"{path.name} ne déclare pas de `permissions:` (least-privilege)"
    )


@pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
def test_workflow_actions_pinned_no_main_no_latest(path: Path):
    """Tranche arbitrage 8 : toutes les actions GHA pinned sur tag
    semver (@vN), interdiction de @main et @latest."""
    data = _load_yaml(path)
    for use in _extract_action_uses(data):
        # Skip workflow_call internes (ex: ./.github/workflows/ci.yml)
        if use.startswith("./"):
            continue
        assert "@main" not in use, f"{path.name} utilise {use} (interdit)"
        assert "@latest" not in use, f"{path.name} utilise {use} (interdit)"


@pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: p.name)
def test_workflow_actions_from_whitelisted_orgs(path: Path):
    """Tranche 9 : whitelist orgs `actions/, docker/, astral-sh/,
    softprops/, github/, dependabot/`."""
    data = _load_yaml(path)
    for use in _extract_action_uses(data):
        if use.startswith("./"):
            continue
        match = ACTION_REGEX.match(use)
        if match is None:
            # Pas une référence d'action standard — skip (peut être un
            # workflow_call interne avec un format différent).
            continue
        org = match.group(1)
        assert org in ALLOWED_ORGS, f"{path.name} utilise org '{org}' hors whitelist {ALLOWED_ORGS}"
