// NEXYA Load Tests — Helper SSE.
//
// Consomme un stream SSE depuis `/chat/stream` et calcule :
//   - time_to_first_token_ms : délai du 1er chunk de texte
//   - total_duration_ms      : durée totale jusqu'au `event: done`
//   - chunks_count            : nombre de chunks textuels reçus
//
// Reconnaît :
//   `event: chunk` → contenu textuel
//   `event: done`  → fin du stream
//   `event: error` → fin sur erreur
//   `: keepalive`  → ignoré (heartbeat 15s)
//
// Note : k6 n'a pas de support SSE natif côté streaming continu. On
// utilise `http.post(..., { responseType: 'text' })` qui retourne le
// body complet à la fin du stream — c'est suffisant pour mesurer la
// durée totale, mais on N'A PAS time_to_first_token_ms en pure HTTP.
// V2 = utiliser `httpx` Python (Locust) ou `k6/x/sse` (extension).
// V1 = on mesure total_duration et on assume time_to_first_token < total.

import http from "k6/http";

export function streamChat(token, message, expertId, baseUrl) {
    const start = Date.now();
    const res = http.post(
        `${baseUrl}/chat/stream`,
        JSON.stringify({
            message,
            expert_id: expertId || "general",
            history: [{ role: "assistant", content: "Je suis prêt." }],
        }),
        {
            headers: {
                Authorization: `Bearer ${token}`,
                "Content-Type": "application/json",
                Accept: "text/event-stream",
            },
            tags: { endpoint: "chat_stream" },
            timeout: "60s",
        }
    );
    const elapsed = Date.now() - start;
    const stats = parseSse(res.body || "");
    return {
        status: res.status,
        total_duration_ms: elapsed,
        chunks_count: stats.chunks_count,
        ended_with_done: stats.ended_with_done,
        ended_with_error: stats.ended_with_error,
        body_size: (res.body || "").length,
    };
}

export function parseSse(body) {
    let chunks_count = 0;
    let ended_with_done = false;
    let ended_with_error = false;
    if (!body) {
        return { chunks_count: 0, ended_with_done: false, ended_with_error: false };
    }
    const blocks = body.split("\n\n");
    for (const block of blocks) {
        const trimmed = (block || "").trim();
        if (!trimmed) continue;
        if (trimmed.startsWith(":")) continue; // keepalive
        let event_type = "";
        for (const line of trimmed.split("\n")) {
            if (line.startsWith("event:")) {
                event_type = line.slice(6).trim();
            }
        }
        if (event_type === "chunk") chunks_count += 1;
        if (event_type === "done") ended_with_done = true;
        if (event_type === "error") ended_with_error = true;
    }
    return { chunks_count, ended_with_done, ended_with_error };
}
