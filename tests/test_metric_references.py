"""K2 — Anti-dérive K1↔K2 : toute métrique `nexya_*` mentionnée dans
les dashboards JSON et les alertes YAML doit exister dans
`prometheus.py` (sinon le panel/l'alerte ne trouvera jamais de
données).

Ce test croise :
  - la liste des noms exposés par `setup_prometheus()` (récupérée via
    introspection du `CollectorRegistry`)
  - la liste des noms `nexya_*` extraits par regex des fichiers JSON
    dashboards et YAML alerting
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS_DIR = BACKEND_ROOT / "grafana" / "provisioning" / "dashboards"
ALERTING_DIR = BACKEND_ROOT / "grafana" / "provisioning" / "alerting"

# `prometheus_client.CollectorRegistry.collect()` retourne des
# `Metric` dont `.name` est le nom de famille SANS le suffixe `_total`
# pour les Counter (alors que la sortie texte du scrape l'ajoute).
# Pour les Histogram, le scrape expose `_bucket`/`_count`/`_sum` qui
# n'apparaissent pas non plus côté `family.name`. On strip ces 4
# suffixes côté texte parsé pour pouvoir comparer aux noms de famille.
HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum", "_total")

NEXYA_METRIC_REGEX = re.compile(r"\bnexya_[a-z_]+\b")


def _exposed_metric_names() -> set[str]:
    """Récupère les 14 noms NEXYA exposés en appelant setup_prometheus
    sur un registry custom isolé."""
    from app.config import Settings
    from app.core.observability import prometheus as prom

    prom._reset_for_tests()
    cfg = Settings(
        env="development",
        prometheus_enabled=True,
        prometheus_scrape_token="",  # vide en dev OK
    )
    ok = prom.setup_prometheus(cfg)
    assert ok, "setup_prometheus failed in test environment"
    registry = prom.get_registry()
    assert registry is not None
    names: set[str] = set()
    for collector in registry.collect():
        names.add(collector.name)
    prom._reset_for_tests()
    return names


def _extract_nexya_metrics_from_text(text: str) -> set[str]:
    raw = set(NEXYA_METRIC_REGEX.findall(text))
    cleaned: set[str] = set()
    for name in raw:
        for suffix in HISTOGRAM_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        cleaned.add(name)
    return cleaned


def _all_dashboard_files() -> list[Path]:
    return sorted(DASHBOARDS_DIR.glob("*.json"))


def _all_alert_files() -> list[Path]:
    return sorted(ALERTING_DIR.glob("*.yml")) + sorted(ALERTING_DIR.glob("*.yaml"))


def test_exposed_metric_count():
    """Sanity : on attend exactement 14 métriques NEXYA (cf. K1)."""
    names = _exposed_metric_names()
    assert len(names) == 14, (
        f"Expected 14 NEXYA metrics from prometheus.py, got {len(names)}: {sorted(names)}"
    )
    for name in names:
        assert name.startswith("nexya_"), f"Non-prefixed metric: {name}"


def test_dashboards_only_reference_existing_metrics():
    exposed = _exposed_metric_names()
    referenced: set[str] = set()
    for path in _all_dashboard_files():
        text = path.read_text(encoding="utf-8")
        referenced |= _extract_nexya_metrics_from_text(text)
    phantoms = referenced - exposed
    assert not phantoms, (
        f"Métriques fantômes dans dashboards (pas exposées par prometheus.py): {phantoms}"
    )


def test_alert_rules_only_reference_existing_metrics():
    exposed = _exposed_metric_names()
    referenced: set[str] = set()
    for path in _all_alert_files():
        text = path.read_text(encoding="utf-8")
        referenced |= _extract_nexya_metrics_from_text(text)
    phantoms = referenced - exposed
    assert not phantoms, (
        f"Métriques fantômes dans alertes (pas exposées par prometheus.py): {phantoms}"
    )


@pytest.mark.parametrize(
    "path",
    _all_dashboard_files(),
    ids=lambda p: p.name,
)
def test_dashboard_references_at_least_one_nexya_metric(path: Path):
    text = path.read_text(encoding="utf-8")
    refs = _extract_nexya_metrics_from_text(text)
    # `04_observability_self.json` mélange métriques NEXYA + métriques
    # standard (up, scrape_duration_seconds, prometheus_tsdb_head_series,
    # process_cpu_seconds_total) — on tolère un seul nexya_ pour ce
    # dashboard, les autres dashboards doivent en avoir plusieurs.
    if path.name == "04_observability_self.json":
        assert len(refs) >= 1, f"{path.name} ne référence aucune métrique NEXYA"
    else:
        assert len(refs) >= 2, f"{path.name} référence trop peu de métriques NEXYA: {refs}"


def test_critical_metrics_covered_in_alerts():
    """Les 6 alertes calibrées doivent référencer les métriques
    cœur (noms de famille, post-strip `_total`/`_bucket`...).
    """
    expected_core_metrics = {
        "nexya_ai_provider_failures",
        "nexya_ai_chat_calls",
        "nexya_ai_chat_total_duration_seconds",
        "nexya_ai_circuit_breaker_state",
        "nexya_notifications_fcm_failures",
        "nexya_notifications_dispatched",
        "nexya_arq_jobs",
        "nexya_ai_cost_usd",
    }
    referenced: set[str] = set()
    for path in _all_alert_files():
        text = path.read_text(encoding="utf-8")
        referenced |= _extract_nexya_metrics_from_text(text)
    missing = expected_core_metrics - referenced
    assert not missing, f"Métriques critiques non couvertes par une alerte: {missing}"


def test_all_dashboards_combined_cover_all_categories():
    """Les 5 dashboards combinés doivent couvrir : ai, tools,
    notifications, arq workers, cache (5 catégories)."""
    referenced: set[str] = set()
    for path in _all_dashboard_files():
        text = path.read_text(encoding="utf-8")
        referenced |= _extract_nexya_metrics_from_text(text)
    categories_ok = {
        "ai": any("nexya_ai_" in m for m in referenced),
        "tools": any("nexya_tools_" in m for m in referenced),
        "notifications": any("nexya_notifications_" in m for m in referenced),
        "arq": any("nexya_arq_" in m for m in referenced),
        "cache": any("nexya_cache_" in m for m in referenced),
    }
    missing = [k for k, v in categories_ok.items() if not v]
    assert not missing, f"Catégories manquantes dans dashboards: {missing}"
