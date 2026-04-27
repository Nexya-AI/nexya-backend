// NEXYA Load Test — GET /metrics (Prometheus scrape).
//
// Scénario : 200 RPS pendant 30s sur l'endpoint Prometheus /metrics.
// Vérifie que le scrape reste rapide même sous charge — un scrape qui
// timeout en prod = trou dans Grafana K2 + alertes manquantes.
//
// Cible SLO : p95 < 100ms, error rate < 0.1%.

import { check } from "k6";
import http from "k6/http";
import { tMetricsScrapeMs } from "../lib/metrics.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";
const TOKEN = __ENV.PROMETHEUS_SCRAPE_TOKEN || "";

export const options = {
    scenarios: {
        metrics_burst: {
            executor: "constant-arrival-rate",
            rate: 200,
            timeUnit: "1s",
            duration: "30s",
            preAllocatedVUs: 100,
            maxVUs: 200,
        },
    },
    thresholds: {
        "metrics_scrape_ms": ["p(95)<100"],
        "http_req_failed{endpoint:prometheus_metrics}": ["rate<0.001"],
    },
};

export default function () {
    const url = TOKEN
        ? `${BASE}/metrics?token=${encodeURIComponent(TOKEN)}`
        : `${BASE}/metrics`;
    const start = Date.now();
    const res = http.get(url, {
        tags: { endpoint: "prometheus_metrics" },
    });
    const elapsed = Date.now() - start;
    tMetricsScrapeMs.add(elapsed);

    check(res, {
        "metrics status 200": (r) => r.status === 200,
        "metrics body non-empty": (r) => (r.body || "").length > 100,
    });
}
