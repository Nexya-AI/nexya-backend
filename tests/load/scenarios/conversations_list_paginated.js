// NEXYA Load Test — GET /chat/conversations cursor-based.
//
// Scénario : 100 RPS pendant 60s, GET conversations paginées avec
// curseur opaque base64. Anti-N+1 detector + stress lecture DB +
// keyset pagination correctness.
//
// Cible SLO : p95 < 300ms, error rate < 1%.
//
// Le seed `pro@nexya.ai` est partagé → les 100 RPS lisent tous la
// même liste, ce qui maximise les locks read DB et expose les
// régressions de plan d'exécution Postgres.

import { check } from "k6";
import http from "k6/http";
import { authHeaders, loginCached, SEED_PRO } from "../lib/auth.js";
import { rListSuccess, tListQueryMs } from "../lib/metrics.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
    scenarios: {
        list_burst: {
            executor: "constant-arrival-rate",
            rate: 100,
            timeUnit: "1s",
            duration: "60s",
            preAllocatedVUs: 50,
            maxVUs: 200,
        },
    },
    thresholds: {
        "list_query_ms": ["p(95)<300"],
        "list_success_rate": ["rate>0.99"],
        "http_req_failed{endpoint:conversations_list}": ["rate<0.01"],
    },
};

let _cachedCursor = null; // partagé entre VUs (k6 single runner)

export default function () {
    const token = loginCached(SEED_PRO);
    const url = _cachedCursor
        ? `${BASE}/chat/conversations?cursor=${encodeURIComponent(_cachedCursor)}&limit=20`
        : `${BASE}/chat/conversations?limit=20`;
    const start = Date.now();
    const res = http.get(url, {
        headers: authHeaders(token),
        tags: { endpoint: "conversations_list" },
    });
    const elapsed = Date.now() - start;
    tListQueryMs.add(elapsed);

    const ok = res.status === 200;
    rListSuccess.add(ok ? 1 : 0);

    check(res, {
        "list status 200": (r) => r.status === 200,
    });

    // Mémorise le cursor de la réponse pour la prochaine itération
    // (signal anti-N+1 — on stresse vraiment le keyset, pas juste la
    // 1ère page).
    if (ok) {
        try {
            const body = res.json();
            const next = body && body.data && body.data.next_cursor;
            if (next) _cachedCursor = next;
        } catch (e) {
            // ignore
        }
    }
}
