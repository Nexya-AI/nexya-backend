"""K2 — Validation des règles d'alerte Prometheus YAML.

Vérifie :
- YAML valide
- structure groups[].rules[].{alert, expr, for, labels, annotations}
- severity ∈ {warning, critical}
- summary + description présents
- for >= 1m (anti-flapping)
- expressions PromQL non-vides référençant nexya_*
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ALERTING_DIR = Path(__file__).resolve().parents[1] / "grafana" / "provisioning" / "alerting"
RULES_FILE = ALERTING_DIR / "rules.yml"

ALLOWED_SEVERITIES = {"warning", "critical"}
DURATION_REGEX = re.compile(r"^(\d+)(s|m|h|d)$")


def _parse_duration_seconds(value: str) -> int:
    """Parse Prometheus duration like '5m', '1h', '30s' into seconds."""
    match = DURATION_REGEX.match(value)
    assert match is not None, f"Invalid duration: {value}"
    n, unit = int(match.group(1)), match.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _load_rules() -> dict:
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))


def test_rules_file_exists():
    assert RULES_FILE.is_file(), f"Missing rules file: {RULES_FILE}"


def test_rules_yaml_valid():
    data = _load_rules()
    assert isinstance(data, dict)
    assert "groups" in data
    assert isinstance(data["groups"], list)
    assert len(data["groups"]) >= 1


def test_rules_group_structure():
    data = _load_rules()
    for group in data["groups"]:
        assert "name" in group
        assert "rules" in group
        assert isinstance(group["rules"], list)


def test_six_alerts_defined():
    data = _load_rules()
    all_alerts = [rule for group in data["groups"] for rule in group["rules"] if "alert" in rule]
    assert len(all_alerts) == 6, f"Expected 6 alerts, found {len(all_alerts)}"


def _all_alert_rules() -> list[dict]:
    data = _load_rules()
    return [rule for group in data["groups"] for rule in group["rules"] if "alert" in rule]


@pytest.mark.parametrize(
    "rule",
    _all_alert_rules(),
    ids=lambda r: r.get("alert", "?"),
)
def test_alert_required_fields(rule: dict):
    required = {"alert", "expr", "for", "labels", "annotations"}
    missing = required - set(rule.keys())
    assert not missing, f"Alert {rule.get('alert')} missing: {missing}"


@pytest.mark.parametrize(
    "rule",
    _all_alert_rules(),
    ids=lambda r: r.get("alert", "?"),
)
def test_alert_severity_valid(rule: dict):
    severity = rule["labels"].get("severity")
    assert severity in ALLOWED_SEVERITIES, (
        f"Alert {rule['alert']} severity '{severity}' not in {ALLOWED_SEVERITIES}"
    )


@pytest.mark.parametrize(
    "rule",
    _all_alert_rules(),
    ids=lambda r: r.get("alert", "?"),
)
def test_alert_has_summary_and_description(rule: dict):
    annotations = rule["annotations"]
    assert "summary" in annotations and annotations["summary"].strip()
    assert "description" in annotations and annotations["description"].strip()


@pytest.mark.parametrize(
    "rule",
    _all_alert_rules(),
    ids=lambda r: r.get("alert", "?"),
)
def test_alert_for_min_1m_anti_flapping(rule: dict):
    seconds = _parse_duration_seconds(rule["for"])
    assert seconds >= 60, f"Alert {rule['alert']} for={rule['for']} < 1 min (flapping risk)"


@pytest.mark.parametrize(
    "rule",
    _all_alert_rules(),
    ids=lambda r: r.get("alert", "?"),
)
def test_alert_expr_non_empty_and_references_nexya(rule: dict):
    expr = rule["expr"]
    assert expr.strip(), f"Alert {rule['alert']} has empty expr"
    assert "nexya_" in expr, f"Alert {rule['alert']} ne référence aucune métrique NEXYA"


def test_expected_alert_names_present():
    """Les 6 alertes attendues sont définies (pas de drift de noms)."""
    expected = {
        "Nexya5xxRateHigh",
        "NexyaChatLatencyHigh",
        "NexyaBreakerOpen",
        "NexyaFCMFailureRateHigh",
        "NexyaArqFailureRateHigh",
        "NexyaCostUSDDailyExceeded",
    }
    actual = {rule["alert"] for rule in _all_alert_rules()}
    assert actual == expected, f"Alert names mismatch: {actual} vs {expected}"
