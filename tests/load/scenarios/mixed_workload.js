// NEXYA Load Test — Mixed workload (signal global prod-like).
//
// Scénario : 30 VUs pendant 5min en `ramping-arrival-rate`, mix
// pondéré 60% chat / 30% list / 10% upload. Le profil de charge le
// plus proche d'un usage prod réel sur l'app NEXYA.
//
// Cible SLO globale : error rate < 1%, p95 http_req_duration < 5s
// (toutes opérations confondues), pas de 5xx.
//
// C'est ce scénario qu'Ivan regardera en premier après chaque PR
// significatif côté infra (DB pool, middleware, retry layer…).

import { check } from "k6";
import http from "k6/http";
import { authHeaders, loginCached, SEED_PRO } from "../lib/auth.js";
import { streamChat } from "../lib/sse.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
    scenarios: {
        mixed: {
            executor: "ramping-arrival-rate",
            startRate: 0,
            timeUnit: "1s",
            preAllocatedVUs: 30,
            maxVUs: 60,
            stages: [
                { duration: "30s", target: 10 },   // warm-up
                { duration: "3m", target: 30 },    // plateau
                { duration: "30s", target: 30 },   // plateau soutenu
                { duration: "30s", target: 0 },    // ramp-down
            ],
        },
    },
    thresholds: {
        "http_req_duration": ["p(95)<5000"],
        "http_req_failed": ["rate<0.01"],
        "checks": ["rate>0.99"],
    },
};

export default function () {
    const token = loginCached(SEED_PRO);
    const dice = Math.random();

    if (dice < 0.6) {
        // 60% — chat stream
        const result = streamChat(
            token,
            "Donne-moi un conseil rapide pour économiser cette semaine.",
            "general",
            BASE
        );
        check(result, {
            "[mixed] chat 200": (r) => r.status === 200,
            "[mixed] chat done": (r) => r.ended_with_done,
        });
    } else if (dice < 0.9) {
        // 30% — list conversations
        const res = http.get(`${BASE}/chat/conversations?limit=10`, {
            headers: authHeaders(token),
            tags: { endpoint: "conversations_list" },
        });
        check(res, {
            "[mixed] list 200": (r) => r.status === 200,
        });
    } else {
        // 10% — upload (small, ~50KB pour ne pas saturer la BP du runner)
        const body = "x".repeat(50 * 1024);
        const res = http.post(
            `${BASE}/files/upload`,
            { file: http.file(body, `mixed-${__VU}-${__ITER}.txt`, "text/plain") },
            {
                headers: { Authorization: `Bearer ${token}` },
                tags: { endpoint: "files_upload" },
                timeout: "10s",
            }
        );
        check(res, {
            "[mixed] upload 200/201": (r) => [200, 201].includes(r.status),
        });
    }
}
