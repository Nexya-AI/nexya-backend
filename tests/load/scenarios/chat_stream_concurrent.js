// NEXYA Load Test — Chat stream SSE concurrent.
//
// Scénario : 30 VUs pendant 5min, chaque VU lance un POST /chat/stream
// SSE et consomme tous les events jusqu'à `event: done`. LLM = mock
// auto (pas de facture Gemini, on teste la chaîne HTTP/DB/Redis).
//
// Cible SLO : p95 total stream < 30s (assistant complète vite avec
// mock), error rate < 1%, ratio streams complétés / démarrés > 99%.

import { check } from "k6";
import { loginCached, SEED_FREE } from "../lib/auth.js";
import { streamChat } from "../lib/sse.js";
import {
    cChatCompleted,
    cChatFailed,
    cChatStarted,
    rChatStreamSuccess,
    tChatChunksCount,
    tChatTotalMs,
} from "../lib/metrics.js";

const BASE = __ENV.BASE_URL || "http://localhost:8000";

const PROMPTS = [
    "Donne-moi une recette simple de poulet DG.",
    "Explique-moi la complexité du tri rapide en Python.",
    "Quelle est la capitale du Cameroun ?",
    "Traduis en anglais : « J'aimerais réserver une table. »",
    "Calcule la TVA à 19,25 % sur 50 000 FCFA HT.",
];

export const options = {
    scenarios: {
        chat_stream: {
            executor: "constant-vus",
            vus: 30,
            duration: "5m",
        },
    },
    thresholds: {
        "chat_total_duration_ms": ["p(95)<30000"],
        "chat_stream_success_rate": ["rate>0.99"],
        "http_req_failed{endpoint:chat_stream}": ["rate<0.01"],
    },
};

export default function () {
    const token = loginCached(SEED_FREE);
    const prompt = PROMPTS[Math.floor(Math.random() * PROMPTS.length)];

    cChatStarted.add(1);
    const result = streamChat(token, prompt, "general", BASE);
    tChatTotalMs.add(result.total_duration_ms);
    tChatChunksCount.add(result.chunks_count);

    const ok = result.status === 200 && result.ended_with_done && !result.ended_with_error;
    rChatStreamSuccess.add(ok ? 1 : 0);
    if (ok) {
        cChatCompleted.add(1);
    } else {
        cChatFailed.add(1);
    }

    check(result, {
        "stream returned 200": (r) => r.status === 200,
        "stream ended with done": (r) => r.ended_with_done,
        "stream got chunks": (r) => r.chunks_count > 0,
    });
}
