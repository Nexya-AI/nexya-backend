"""K2 — Validation structure des dashboards Grafana JSON.

Chaque test charge les 5 dashboards JSON et vérifie :
- syntaxe JSON valide
- shape minimale (uid, title, schemaVersion, panels, time, refresh)
- UIDs uniques cross-dashboards
- chaque panel a id, type, title, gridPos, targets
- schemaVersion >= 39 (Grafana 10+ alerting natif)
- refresh interval valide
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DASHBOARDS_DIR = Path(__file__).resolve().parents[1] / "grafana" / "provisioning" / "dashboards"

EXPECTED_UIDS = {
    "nexya-overview",
    "nexya-ai",
    "nexya-tools-notifications",
    "nexya-workers",
    "nexya-self",
}

VALID_REFRESH_INTERVALS = {"5s", "10s", "30s", "1m", "5m", "15m", "30m", "1h"}


def _all_dashboard_files() -> list[Path]:
    return sorted(DASHBOARDS_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_dashboards_directory_exists():
    assert DASHBOARDS_DIR.is_dir(), f"Missing dashboards dir: {DASHBOARDS_DIR}"


def test_five_dashboard_files_present():
    files = _all_dashboard_files()
    assert len(files) == 5, f"Expected 5 dashboards, found {len(files)}: {files}"


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_is_valid_json(path: Path):
    data = _load(path)
    assert isinstance(data, dict)


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_has_required_top_level_keys(path: Path):
    data = _load(path)
    required = {"uid", "title", "schemaVersion", "panels", "time", "refresh"}
    missing = required - set(data.keys())
    assert not missing, f"{path.name} missing keys: {missing}"


def test_dashboard_uids_unique_and_match_expected():
    uids = []
    for path in _all_dashboard_files():
        uids.append(_load(path)["uid"])
    assert len(uids) == len(set(uids)), f"Duplicate UIDs: {uids}"
    assert set(uids) == EXPECTED_UIDS, f"UID mismatch: {set(uids)} vs {EXPECTED_UIDS}"


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_schema_version_grafana_10_plus(path: Path):
    data = _load(path)
    assert data["schemaVersion"] >= 39, (
        f"{path.name} schemaVersion {data['schemaVersion']} < 39 (Grafana 10+ requis)"
    )


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_refresh_interval_valid(path: Path):
    data = _load(path)
    assert data["refresh"] in VALID_REFRESH_INTERVALS, (
        f"{path.name} refresh '{data['refresh']}' invalide"
    )


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_has_panels(path: Path):
    data = _load(path)
    assert isinstance(data["panels"], list)
    assert len(data["panels"]) >= 1, f"{path.name} a 0 panel"
    assert len(data["panels"]) <= 12, (
        f"{path.name} a {len(data['panels'])} panels (>12 = anti-pattern surcharge)"
    )


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_panels_have_required_keys(path: Path):
    data = _load(path)
    panel_required = {"id", "type", "title", "gridPos", "targets"}
    for panel in data["panels"]:
        missing = panel_required - set(panel.keys())
        assert not missing, f"{path.name} panel '{panel.get('title', '?')}' missing: {missing}"
        assert isinstance(panel["targets"], list)
        assert len(panel["targets"]) >= 1


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_panel_ids_unique_within_dashboard(path: Path):
    data = _load(path)
    ids = [panel["id"] for panel in data["panels"]]
    assert len(ids) == len(set(ids)), f"{path.name} panel IDs dupliqués: {ids}"


@pytest.mark.parametrize("path", _all_dashboard_files(), ids=lambda p: p.name)
def test_dashboard_panel_targets_have_expr_and_refid(path: Path):
    data = _load(path)
    for panel in data["panels"]:
        for target in panel["targets"]:
            assert "expr" in target, f"{path.name} panel '{panel['title']}' target sans expr"
            assert "refId" in target, f"{path.name} panel '{panel['title']}' target sans refId"
            assert target["expr"].strip(), f"{path.name} panel '{panel['title']}' a une expr vide"
