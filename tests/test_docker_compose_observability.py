"""K2 — Validation docker-compose.observability.yml + prometheus.yml."""

from __future__ import annotations

from pathlib import Path

import yaml

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
COMPOSE_FILE = DOCKER_DIR / "docker-compose.observability.yml"
PROMETHEUS_CONFIG = DOCKER_DIR / "prometheus" / "prometheus.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_compose_yaml_valid():
    data = _load(COMPOSE_FILE)
    assert isinstance(data, dict)
    assert "services" in data
    assert "volumes" in data
    assert "networks" in data


def test_prometheus_and_grafana_services_present():
    data = _load(COMPOSE_FILE)
    assert "prometheus" in data["services"]
    assert "grafana" in data["services"]


def test_image_versions_pinned_no_latest():
    data = _load(COMPOSE_FILE)
    for name, svc in data["services"].items():
        image = svc.get("image", "")
        assert ":" in image, f"{name} image '{image}' sans tag"
        tag = image.split(":")[-1]
        assert tag != "latest", f"{name} utilise :latest (anti-pattern reproducibilité)"


def test_grafana_security_envs_set():
    data = _load(COMPOSE_FILE)
    env = data["services"]["grafana"]["environment"]
    assert env["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert env["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert "GF_SECURITY_ADMIN_PASSWORD" in env


def test_prometheus_volumes_mount_config_readonly():
    data = _load(COMPOSE_FILE)
    vols = data["services"]["prometheus"]["volumes"]
    config_mount = next((v for v in vols if "prometheus.yml" in v), None)
    assert config_mount is not None
    assert config_mount.endswith(":ro"), (
        "Prometheus config doit être monté :ro pour éviter modifs runtime"
    )


def test_grafana_provisioning_volume_readonly():
    data = _load(COMPOSE_FILE)
    vols = data["services"]["grafana"]["volumes"]
    prov_mount = next((v for v in vols if "provisioning" in v), None)
    assert prov_mount is not None
    assert prov_mount.endswith(":ro")


def test_prometheus_yml_valid_and_scrapes_backend():
    data = _load(PROMETHEUS_CONFIG)
    assert data["global"]["scrape_interval"] == "15s"
    backend_job = next(
        (job for job in data["scrape_configs"] if job["job_name"] == "nexya-backend"),
        None,
    )
    assert backend_job is not None
    assert backend_job["metrics_path"] == "/metrics"
    targets = backend_job["static_configs"][0]["targets"]
    assert "nexya-backend:8000" in targets


def test_prometheus_yml_loads_alert_rules():
    data = _load(PROMETHEUS_CONFIG)
    assert "rule_files" in data
    assert any("rules" in str(rf) for rf in data["rule_files"]), (
        "rule_files doit charger les règles d'alerte"
    )
