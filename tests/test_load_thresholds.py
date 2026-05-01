"""
Tests N4 — validation structure des fichiers k6 + thresholds.json.

On NE LANCE PAS k6 ici (trop lourd, demande docker + binary k6) — on
valide juste la cohérence des YAML/JS/JSON :
1. `thresholds.json` est valide + contient les 6 scénarios attendus
2. Chaque entrée a p50/p95/p99 + error_rate_max
3. Chaque scénario .js existe sur disque
4. Cross-check noms de scénarios YAML ↔ fichiers
5. `docker-compose.load.yml` est YAML valide
6. `bootstrap.sh` + `teardown.sh` + `run.sh` sont du bash valide (`bash -n`)
7. `.github/workflows/load.yml` est YAML valide + cite les scénarios attendus

Les load tests réels tournent en `workflow_dispatch` ou `schedule` —
pas dans la suite pytest principale.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

LOAD_DIR = Path(__file__).parent / "load"
SCENARIOS_DIR = LOAD_DIR / "scenarios"
DOCKER_DIR = LOAD_DIR / "docker"
THRESHOLDS_PATH = LOAD_DIR / "thresholds.json"
COMPOSE_PATH = DOCKER_DIR / "docker-compose.load.yml"
WORKFLOW_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "load.yml"

EXPECTED_SCENARIOS: tuple[str, ...] = (
    "auth_burst",
    "chat_stream_concurrent",
    "files_upload_concurrent",
    "conversations_list_paginated",
    "metrics_endpoint",
    "mixed_workload",
)


# ══════════════════════════════════════════════════════════════
# thresholds.json
# ══════════════════════════════════════════════════════════════


def test_thresholds_json_is_valid_json() -> None:
    assert THRESHOLDS_PATH.exists()
    data = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "scenarios" in data


def test_thresholds_contains_all_expected_scenarios() -> None:
    data = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    declared = set(data["scenarios"].keys())
    expected = set(EXPECTED_SCENARIOS)
    missing = expected - declared
    assert not missing, f"Scénarios sans threshold : {missing}"


@pytest.mark.parametrize("scenario", EXPECTED_SCENARIOS)
def test_each_scenario_has_error_rate_cap(scenario: str) -> None:
    data = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    block = data["scenarios"][scenario]
    assert "error_rate_max" in block, f"{scenario} sans error_rate_max"
    cap = block["error_rate_max"]
    assert isinstance(cap, (int, float))
    assert 0.0 <= cap <= 0.1, f"{scenario} error_rate_max = {cap} (hors [0, 0.1])"


def test_at_least_one_p95_threshold_per_scenario() -> None:
    data = json.loads(THRESHOLDS_PATH.read_text(encoding="utf-8"))
    for scenario in EXPECTED_SCENARIOS:
        block = data["scenarios"][scenario]
        # Cherche au moins un dict avec une clé `p95_ms` à l'intérieur du block.
        has_p95 = any(isinstance(v, dict) and "p95_ms" in v for v in block.values())
        assert has_p95, f"{scenario} sans aucun p95_ms"


# ══════════════════════════════════════════════════════════════
# Fichiers .js scenarios
# ══════════════════════════════════════════════════════════════


def test_scenarios_dir_exists() -> None:
    assert SCENARIOS_DIR.is_dir()


@pytest.mark.parametrize("scenario", EXPECTED_SCENARIOS)
def test_scenario_js_file_exists(scenario: str) -> None:
    path = SCENARIOS_DIR / f"{scenario}.js"
    assert path.exists(), f"{path} manquant"
    content = path.read_text(encoding="utf-8")
    # Sanity : doit contenir `export const options` et `export default`
    assert "export const options" in content, f"{scenario}.js sans export options"
    assert "export default" in content, f"{scenario}.js sans export default"


def test_scenarios_dir_has_no_extra_files() -> None:
    """Anti-dérive — scénarios disque ⊆ scénarios attendus.

    Si quelqu'un ajoute un .js sans mettre à jour EXPECTED_SCENARIOS,
    le test casse. Force la maintenance synchronisée du registre.
    """
    actual = {p.stem for p in SCENARIOS_DIR.glob("*.js")}
    extra = actual - set(EXPECTED_SCENARIOS)
    assert not extra, f"Scénarios non listés : {extra}"


# ══════════════════════════════════════════════════════════════
# docker-compose.load.yml
# ══════════════════════════════════════════════════════════════


def test_docker_compose_yaml_is_valid() -> None:
    assert COMPOSE_PATH.exists()
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert "services" in data
    services = data["services"]
    assert "postgres" in services
    assert "redis" in services
    assert "minio" in services
    assert "backend" in services


def test_docker_compose_uses_pinned_images() -> None:
    """Anti-pattern :latest — chaque image doit avoir un tag fixé."""
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    for name, svc in data["services"].items():
        if "image" in svc:
            image = svc["image"]
            assert ":latest" not in image, f"{name} utilise :latest"
            assert ":" in image, f"{name} sans tag explicite"


# ══════════════════════════════════════════════════════════════
# Bash scripts (bash -n syntaxe)
# ══════════════════════════════════════════════════════════════


def _bash_or_skip() -> str | None:
    """Retourne le path bash ou None si absent (Windows sans WSL)."""
    return shutil.which("bash")


@pytest.mark.parametrize(
    "script_path",
    [
        DOCKER_DIR / "bootstrap.sh",
        DOCKER_DIR / "teardown.sh",
        LOAD_DIR / "run.sh",
    ],
)
def test_bash_script_has_valid_syntax(script_path: Path) -> None:
    bash = _bash_or_skip()
    if bash is None:
        pytest.skip("bash absent (Windows sans WSL)")
    assert script_path.exists()
    result = subprocess.run(
        [bash, "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"{script_path} bash syntax error: {result.stderr}"


@pytest.mark.parametrize(
    "script_path",
    [
        DOCKER_DIR / "bootstrap.sh",
        DOCKER_DIR / "teardown.sh",
        LOAD_DIR / "run.sh",
    ],
)
def test_bash_script_uses_strict_mode(script_path: Path) -> None:
    """Chaque script bash NEXYA doit utiliser `set -euo pipefail`."""
    content = script_path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content, f"{script_path.name} sans set -euo pipefail"


# ══════════════════════════════════════════════════════════════
# .github/workflows/load.yml
# ══════════════════════════════════════════════════════════════


def test_load_workflow_yaml_is_valid() -> None:
    assert WORKFLOW_PATH.exists()
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    # YAML loader convertit le mot-clé `on` en booléen True (oui/yes/on)
    # → on cherche soit "on" string soit True bool
    has_on = "on" in data or True in data
    assert has_on, "Workflow sans clé 'on'"


def test_load_workflow_has_workflow_dispatch_and_schedule() -> None:
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in raw
    assert "schedule:" in raw
    assert "cron:" in raw


def test_load_workflow_lists_expected_scenarios() -> None:
    """Le dropdown `workflow_dispatch.inputs.scenario.options` doit
    contenir les 6 scénarios attendus + 'all'."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "- all" in raw
    for scenario in EXPECTED_SCENARIOS:
        assert f"- {scenario}" in raw, f"Scenario {scenario} absent du dropdown workflow_dispatch"


def test_load_workflow_has_issue_creation_on_breach() -> None:
    """Anti-régression : le workflow doit créer une issue si breach."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "github-script" in raw
    assert "issues.create" in raw
    assert "load-regression" in raw


def test_load_workflow_uploads_artifacts() -> None:
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/upload-artifact" in raw
    assert "load-reports" in raw


# ══════════════════════════════════════════════════════════════
# lib k6 partagée
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "lib_file",
    ["auth.js", "sse.js", "metrics.js"],
)
def test_lib_k6_files_exist(lib_file: str) -> None:
    path = LOAD_DIR / "lib" / lib_file
    assert path.exists(), f"lib/{lib_file} manquant"
    content = path.read_text(encoding="utf-8")
    assert "export" in content, f"lib/{lib_file} sans export (pas un module)"
