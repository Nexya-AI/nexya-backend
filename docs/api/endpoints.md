# API Endpoints — NEXYA Backend

> **Executive summary (EN).** Exhaustive table of 60 NEXYA backend
> endpoints, grouped by feature. Source of truth: [`openapi.json`](openapi.json)
> (regenerate via `python -m scripts.export_openapi`). All authenticated
> endpoints require JWT RS256 Bearer token. Rate limits documented per
> endpoint. Error codes: see [`error-codes.md`](error-codes.md).

> Régénérer ce fichier : à partir de `openapi.json` (cf. workflow CI
> `dd-exports-fresh.yml`).

---

## Auth (`/auth/*`)

| Méthode | Path | Auth | Rate limit | Description |
|---|---|---|---|---|
| POST | `/auth/register` | — | 5/min/IP + 5/jour/IP + 5/jour/device + captcha | Inscription (mock-first hCaptcha) |
| POST | `/auth/login` | — | 10/min/IP | Connexion (bcrypt verify) |
| POST | `/auth/refresh` | refresh_token body | — | Rotation refresh + nouveau access |
| POST | `/auth/logout` | JWT | — | Blacklist access jti + révoque tous refresh |
| POST | `/auth/forgot-password` | — | 10/h/IP + 3/h/email | Email reset link (anti-enumeration 200 générique) |
| POST | `/auth/reset-password` | reset_token body | 5/h/IP | Token JWT TTL 15 min + fingerprint hash |

## User (`/user/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| GET | `/user/profile` | JWT | Profil user courant |
| PUT | `/user/profile` | JWT | Update partiel (display_name, bio, locale, voice_id) |
| PUT | `/user/password` | JWT | Change password (révoque tous refresh) |
| DELETE | `/user/account` | JWT | RGPD anonymisation logique |
| POST | `/user/device-token` | JWT | Enregistrement FCM token |
| DELETE | `/user/device-token` | JWT | Désactivation FCM token |
| GET | `/user/notification-preferences` | JWT | 5 catégories RGPD avec defaults |
| PUT | `/user/notification-preferences` | JWT | UPSERT partial (push/email/both/none) |

## Chat (`/chat/*`)

| Méthode | Path | Auth | Rate limit | Description |
|---|---|---|---|---|
| POST | `/chat/stream` | JWT | budget chat | SSE streaming (heartbeat 15s + cancel duale) |
| POST | `/chat/stop` | JWT | — | Pose Redis key chat:cancel:{session_id} |
| POST | `/chat/reports` | JWT | 10/h/user | Signalement message (UNIQUE composite anti-doublon) |
| POST | `/chat/messages/{message_id}/feedback` | JWT | 60/h/user | Thumbs up/down (UPSERT atomique) |
| DELETE | `/chat/messages/{message_id}/feedback` | JWT | — | Annule (204 idempotent) |
| GET | `/chat/conversations` | JWT | — | Keyset paginé + FTS française `?q=` |
| POST | `/chat/conversations` | JWT | — | Création conversation |
| GET | `/chat/conversations/{id}` | JWT | — | Détail (404 IDOR-safe) |
| PATCH | `/chat/conversations/{id}` | JWT | — | Update (rename, archive, fav) |
| DELETE | `/chat/conversations/{id}` | JWT | — | Soft-delete |
| GET | `/chat/conversations/{id}/messages` | JWT | — | Cursor ASC |
| GET | `/chat/conversations/trash` | JWT | — | Liste corbeille |
| POST | `/chat/conversations/{id}/restore` | JWT | — | Restaurer depuis corbeille |
| DELETE | `/chat/conversations/{id}/permanent` | JWT | — | Hard delete (cascade SQL) |

## Projects (`/projects/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| POST | `/projects` | JWT | Création (quota Free=3 / Pro=50) |
| GET | `/projects` | JWT | Keyset paginé + `?q=` trigram |
| GET | `/projects/{id}` | JWT | Détail + counts (file/conversation) |
| PATCH | `/projects/{id}` | JWT | Update + `clear_instructions` flag |
| DELETE | `/projects/{id}` | JWT | Soft-delete + détache conversations |
| GET | `/projects/{id}/conversations` | JWT | Conversations filtrées project_id |
| POST | `/projects/{id}/files` | JWT | Attache via upload_id (E3) |
| GET | `/projects/{id}/files` | JWT | Keyset paginé |
| DELETE | `/projects/{id}/files/{file_id}` | JWT | Soft-delete idempotent |

## Library (`/library/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| POST | `/library` | JWT | Upload base64 ≤ 20 MB (dédup SHA-256) |
| GET | `/library` | JWT | Keyset + filtres (type/source/conv_id) + `?q=` |
| GET | `/library/{id}` | JWT | Presigned URL TTL 1h |
| DELETE | `/library/{id}` | JWT | Soft-delete (suppression MinIO différée) |

## Files (`/files/*`)

| Méthode | Path | Auth | Rate limit | Description |
|---|---|---|---|---|
| POST | `/files/upload` | JWT | 20/h/user | Pipeline 10 étapes (whitelist + magic-bytes + scan virus + extraction texte async) |

## Voice (`/voice/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| GET | `/voice/list` | JWT | Catalogue 6 voix branded (Free + Pro) |
| POST | `/voice/transcribe` | **JWT Pro** | Whisper STT + dédup SHA |
| POST | `/voice/speak` | **JWT Pro** | TTS OpenAI + auto-save Library |

## Vision (`/vision/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| POST | `/vision/analyze` | JWT | Multimodal Gemini Flash/Pro + GPT-4o (Free=flash imposé, Pro choix tier) |

## Memory (`/memory/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| POST | `/memory/index` | JWT | Ajout fait durable (source='manual') |
| POST | `/memory/search` | JWT | Top-K cosinus pgvector + framing RAG D5 |
| GET | `/memory` | JWT | Keyset paginé + filtre source |
| DELETE | `/memory/{id}` | JWT | RGPD hard DELETE (idempotent anti-énumération) |

## RAG (`/rag/*`)

| Méthode | Path | Auth | Rate limit | Description |
|---|---|---|---|---|
| POST | `/rag/query` | JWT | 60/h/user + 1 crédit embeddings | Recherche vectorielle documents + framing anti-prompt-injection |

## AI Models (`/models`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| GET | `/models` | JWT | Inventaire 25+ modèles aggrégé runtime + experts_routing |

## Tasks (`/tasks/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| POST | `/tasks` | JWT | Création (quota Free=3 / Pro=50) — schedule once/interval/daily/weekly |
| GET | `/tasks` | JWT | Keyset paginé + filtre status |
| GET | `/tasks/{id}` | JWT | Détail (404 IDOR) |
| PATCH | `/tasks/{id}` | JWT | Update + recompute next_run_at |
| DELETE | `/tasks/{id}` | JWT | Soft-delete |
| POST | `/tasks/{id}/pause` | JWT | Pause (clear next_run_at) |
| POST | `/tasks/{id}/resume` | JWT | Recompute next_run_at depuis schedule courant |
| GET | `/tasks/{id}/results` | JWT | Historique exécutions keyset |

## Notifications (`/notifications/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| GET | `/notifications` | JWT | Keyset + filtres (unread_only, category) |
| POST | `/notifications/read` | JWT | Bulk idempotent (max 100 IDs) |
| DELETE | `/notifications/{id}` | JWT | Soft-delete (404 IDOR) |
| POST | `/notifications/unsubscribe/{token}` | — (public) | JWT one-click TTL 365j |

## Suggestions (`/suggestions`)

| Méthode | Path | Auth | Rate limit | Description |
|---|---|---|---|---|
| POST | `/suggestions` | JWT | 5/jour/user | Formulaire user → équipe NEXYA (email Brevo fail-safe) |

## RGPD (`/rgpd/*`)

| Méthode | Path | Auth | Rate limit | Description |
|---|---|---|---|---|
| GET | `/rgpd/user/data-export` | JWT | 1/24h/user | ZIP 23 fichiers (Articles 15+20) |
| GET | `/rgpd/user/consent` | JWT | — | Liste consents actifs |
| POST | `/rgpd/user/consent` | JWT | — | Enregistre (idempotent + version-aware) |
| DELETE | `/rgpd/user/consent/{type}` | JWT | — | 204 idempotent (Article 7) |
| POST | `/rgpd/user/account/delete-request` | JWT | — | 202 + 409 si déjà pending (Article 17) |
| POST | `/rgpd/user/account/delete-request/cancel` | JWT | — | 200 + 404 si aucune active |

## Admin (`/admin/*`)

| Méthode | Path | Auth | Description |
|---|---|---|---|
| GET | `/admin/helpdesk/metrics` | `require_admin` | KPI agrégés (open/in_progress/resolved + median age + breakdown) |
| GET | `/rgpd/admin/ai-act-registry?format=csv|json` | `require_admin` | Article 13 AI Act registre `ai_calls` enrichi |

## Health & Observability

| Méthode | Path | Auth | Description |
|---|---|---|---|
| GET | `/healthz` | — (public) | Liveness K8s — pas de check externe |
| GET | `/ready` | — (public) | Readiness étendu O1 (version + db_latency + redis_latency + arq_queue + uptime + last_migration) |
| GET | `/health` | — (public) | Alias `/ready` (backward-compat) |
| GET | `/version` | — (public) | Version git + commit_sha + tag + dirty + env + source |
| GET | `/metrics` | `X-Prometheus-Token` | Prometheus scrape (token constant-time compare) |
| GET | `/observability/status` | `X-Prometheus-Token` | JSON synthèse OTel + Sentry + Prometheus |

## Image Generation

| Méthode | Path | Auth | Description |
|---|---|---|---|
| POST | `/image/generate` | JWT | Imagen 3 + watermark NEXYA visuel + auto-save Library + Pro `remove_watermark` |

---

## Format de réponse uniforme

Tous les endpoints retournent `NexyaResponse[T]` :

**Succès** :
```json
{
  "success": true,
  "data": { ... }
}
```

**Erreur** :
```json
{
  "success": false,
  "error": "Message lisible utilisateur",
  "code": "RATE_LIMIT_EXCEEDED",
  "data": { "retry_after": 1800 }
}
```

Codes HTTP standards : 200 / 201 / 204 / 400 / 401 / 402 / 403 / 404 /
409 / 413 / 415 / 422 / 429 / 503.

Voir [`error-codes.md`](error-codes.md) pour le catalogue complet
des codes NEXYA.

---

## Schéma OpenAPI 3.1 complet

Source : [`openapi.json`](openapi.json) (~6500 lignes).

Régénérer après modification d'un router :
```bash
python -m scripts.export_openapi
```

CI vérifie la fraîcheur via `.github/workflows/dd-exports-fresh.yml`
sur push main.

Visualiser :
- Swagger UI : http://localhost:8000/docs
- ReDoc : http://localhost:8000/redoc
- Postman : importer `openapi.json` dans collection
