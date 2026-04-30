"""L1 — Validation .github/dependabot.yml + .github/release.yml."""

from __future__ import annotations

from pathlib import Path

import yaml

GITHUB_DIR = Path(__file__).resolve().parents[1] / ".github"
DEPENDABOT_FILE = GITHUB_DIR / "dependabot.yml"
RELEASE_NOTES_FILE = GITHUB_DIR / "release.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_dependabot_yaml_valid():
    data = _load(DEPENDABOT_FILE)
    assert data["version"] == 2
    assert "updates" in data
    assert isinstance(data["updates"], list)


def test_dependabot_three_updaters_present():
    data = _load(DEPENDABOT_FILE)
    ecosystems = {u["package-ecosystem"] for u in data["updates"]}
    assert ecosystems == {"pip", "docker", "github-actions"}


def test_dependabot_weekly_interval():
    data = _load(DEPENDABOT_FILE)
    for updater in data["updates"]:
        assert updater["schedule"]["interval"] == "weekly"


def test_dependabot_max_5_prs_pip():
    data = _load(DEPENDABOT_FILE)
    pip_updater = next(u for u in data["updates"] if u["package-ecosystem"] == "pip")
    assert pip_updater["open-pull-requests-limit"] <= 5


def test_dependabot_no_conventional_commits_prefix():
    """Règle NEXYA feedback_git_commits.md : pas de `chore(deps):`,
    on accepte `deps(python):` qui est plus lisible."""
    data = _load(DEPENDABOT_FILE)
    for updater in data["updates"]:
        prefix = updater.get("commit-message", {}).get("prefix", "")
        assert not prefix.startswith("chore"), f"Préfixe Conventional Commits interdit : {prefix}"


def test_release_notes_yaml_valid():
    data = _load(RELEASE_NOTES_FILE)
    assert "changelog" in data
    assert "categories" in data["changelog"]


def test_release_notes_categories_present():
    data = _load(RELEASE_NOTES_FILE)
    titles = {cat["title"] for cat in data["changelog"]["categories"]}
    # Au moins les 4 catégories de base + dependencies
    assert any("Breaking" in t for t in titles)
    assert any("Features" in t for t in titles)
    assert any("Fixes" in t for t in titles)
    assert any("Dependencies" in t for t in titles)


def test_release_notes_excludes_dependabot_label():
    data = _load(RELEASE_NOTES_FILE)
    excluded = data["changelog"].get("exclude", {}).get("labels", [])
    assert "dependabot" in excluded
