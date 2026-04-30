"""K2 — Validation des fichiers de provisioning Grafana YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

PROVISIONING_DIR = Path(__file__).resolve().parents[1] / "grafana" / "provisioning"
DATASOURCES_FILE = PROVISIONING_DIR / "datasources" / "datasources.yml"
DASHBOARDS_FILE = PROVISIONING_DIR / "dashboards" / "dashboards.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_datasources_yaml_valid():
    data = _load(DATASOURCES_FILE)
    assert data["apiVersion"] == 1
    assert "datasources" in data
    assert isinstance(data["datasources"], list)


def test_datasources_prometheus_default_and_locked():
    data = _load(DATASOURCES_FILE)
    prom = next((d for d in data["datasources"] if d.get("type") == "prometheus"), None)
    assert prom is not None, "No Prometheus datasource declared"
    assert prom["url"] == "http://prometheus:9090"
    assert prom["isDefault"] is True
    assert prom["editable"] is False
    assert prom["uid"] == "nexya-prom"


def test_dashboards_provisioning_valid():
    data = _load(DASHBOARDS_FILE)
    assert data["apiVersion"] == 1
    assert "providers" in data
    assert isinstance(data["providers"], list)


def test_dashboards_provider_locked_and_targets_correct_path():
    data = _load(DASHBOARDS_FILE)
    provider = data["providers"][0]
    assert provider["folder"] == "NEXYA"
    assert provider["type"] == "file"
    assert provider["allowUiUpdates"] is False
    assert provider["disableDeletion"] is True
    assert provider["options"]["path"] == "/etc/grafana/provisioning/dashboards"
