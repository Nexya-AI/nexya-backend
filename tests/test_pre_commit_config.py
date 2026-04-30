"""L1 — Validation .pre-commit-config.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml

PRE_COMMIT_FILE = Path(__file__).resolve().parents[1] / ".pre-commit-config.yaml"


def _load() -> dict:
    return yaml.safe_load(PRE_COMMIT_FILE.read_text(encoding="utf-8"))


def test_pre_commit_yaml_valid():
    data = _load()
    assert "repos" in data
    assert isinstance(data["repos"], list)


def test_ruff_hooks_present():
    data = _load()
    hook_ids: set[str] = set()
    for repo in data["repos"]:
        for hook in repo.get("hooks", []):
            hook_ids.add(hook["id"])
    assert "ruff" in hook_ids
    assert "ruff-format" in hook_ids


def test_detect_private_key_hook_present():
    data = _load()
    hook_ids: set[str] = set()
    for repo in data["repos"]:
        for hook in repo.get("hooks", []):
            hook_ids.add(hook["id"])
    assert "detect-private-key" in hook_ids


def test_check_added_large_files_hook_present():
    data = _load()
    found = False
    for repo in data["repos"]:
        for hook in repo.get("hooks", []):
            if hook["id"] == "check-added-large-files":
                found = True
                # Vérifie qu'on a un cap raisonnable (≤ 1 MB)
                args = hook.get("args", [])
                assert any("maxkb" in a for a in args), (
                    "check-added-large-files devrait avoir --maxkb"
                )
    assert found


def test_all_repo_revs_pinned():
    """Toutes les versions de hooks pinned via `rev:` (reproducibilité)."""
    data = _load()
    for repo in data["repos"]:
        # Skip repos `meta` qui n'ont pas de rev.
        if repo["repo"] == "meta":
            continue
        rev = repo.get("rev", "")
        assert rev, f"repo {repo['repo']} sans rev (non-reproducible)"
        # Refuse `main`, `master`, `latest`.
        assert rev not in ("main", "master", "latest"), f"repo {repo['repo']} rev={rev} non-pinned"
