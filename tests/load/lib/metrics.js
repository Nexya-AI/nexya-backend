// NEXYA Load Tests — Custom Trends + Counters alignés noms K1.
//
// k6 expose nativement `http_req_duration`, mais on émet des Trends
// custom alignés sur les noms Prometheus K1 pour cohérence cross-tool :
// quand un dev voit `nexya_ai_chat_first_chunk_seconds` dans Grafana K2,
// il retrouve `chat_first_token_ms` ici sans devoir traduire.
//
// V2 = parser les rapports JSON k6 et les pousser vers le Pushgateway
// Prometheus pour qu'ils apparaissent directement dans K2 Grafana.

import { Trend, Counter, Rate } from "k6/metrics";

// ─── Trends — durée par scénario clé ──────────────────────────
export const tChatTotalMs = new Trend("chat_total_duration_ms", true);
export const tChatChunksCount = new Trend("chat_chunks_count");
export const tUploadTotalMs = new Trend("upload_total_ms", true);
export const tListQueryMs = new Trend("list_query_ms", true);
export const tMetricsScrapeMs = new Trend("metrics_scrape_ms", true);
export const tAuthLoginMs = new Trend("auth_login_ms", true);
export const tAuthRegisterMs = new Trend("auth_register_ms", true);

// ─── Counters — nombre d'événements ───────────────────────────
export const cChatStarted = new Counter("chat_streams_started");
export const cChatCompleted = new Counter("chat_streams_completed");
export const cChatFailed = new Counter("chat_streams_failed");
export const cUploadOk = new Counter("uploads_succeeded");
export const cUploadKo = new Counter("uploads_failed");

// ─── Rates — taux de succès ───────────────────────────────────
export const rChatStreamSuccess = new Rate("chat_stream_success_rate");
export const rUploadSuccess = new Rate("upload_success_rate");
export const rListSuccess = new Rate("list_success_rate");
