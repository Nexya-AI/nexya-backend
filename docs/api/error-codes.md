# Error Codes — NEXYA Backend

> **Executive summary (EN).** All NEXYA error responses follow the
> uniform `NexyaResponse[T]` envelope with `code` field for client
> parsing. 30+ stable error codes grouped by category. HTTP status
> codes follow REST standards. Client (Flutter) parses `code` to
> display localized error messages.

> Source de vérité : [`app/core/errors/exceptions.py`](../../app/core/errors/exceptions.py).

---

## Format de réponse erreur

```json
{
  "success": false,
  "error": "Message lisible (FR)",
  "code": "RATE_LIMIT_EXCEEDED",
  "data": { "retry_after": 1800 }
}
```

`data` est optionnel — utilisé pour passer des infos supplémentaires
parsable côté client (ex: `retry_after`, `current/max/plan` pour
quotas).

---

## Auth & Tokens

| Code | HTTP | Description | `data` |
|---|---|---|---|
| `AUTH_TOKEN_EXPIRED` | 401 | Access token expiré, le client doit refresh | — |
| `AUTH_TOKEN_INVALID` | 401 | Token mal formé / signature KO | — |
| `AUTH_REFRESH_EXPIRED` | 401 | Refresh expiré, redirect login | — |
| `AUTH_CREDENTIALS_INVALID` | 401 | Email ou mot de passe incorrect | — |
| `AUTH_EMAIL_ALREADY_EXISTS` | 409 | Email déjà utilisé à l'inscription | — |
| `AUTH_USERNAME_ALREADY_EXISTS` | 409 | Username déjà utilisé | — |
| `RESET_TOKEN_INVALID` | 400 | Token reset password invalide | — |
| `RESET_TOKEN_EXPIRED` | 400 | Token reset password expiré (TTL 15 min) | — |
| `UNSUBSCRIBE_TOKEN_INVALID` | 400 | Token unsubscribe invalide | — |
| `UNSUBSCRIBE_TOKEN_EXPIRED` | 400 | Token unsubscribe expiré (TTL 365 j) | — |
| `UNSUBSCRIBE_SECURITY_REFUSED` | 400 | Catégorie `security` non-désinscriptible (obligation légale) | — |

## Captcha & Anti-abus

| Code | HTTP | Description | `data` |
|---|---|---|---|
| `CAPTCHA_INVALID` | 400 | hCaptcha rejet | — |
| `DEVICE_QUOTA_EXCEEDED` | 429 | 5/jour/device atteint | — |
| `RATE_LIMIT_IP` | 429 | Trop de requêtes IP | `retry_after` |
| `RATE_LIMIT_EXCEEDED` | 429 | Quota journalier user | `reset_at` |
| `RATE_LIMIT_ABUSE` | 429 | Anti-spam user-scoped | `retry_after` |

## Quotas & Plans

| Code | HTTP | Description | `data` |
|---|---|---|---|
| `PLAN_REQUIRED` | 403 | Feature Pro-only | `feature` |
| `PROJECT_QUOTA_EXCEEDED` | 402 | Quota projets atteint | `current, max, plan` |
| `PROJECT_FILES_QUOTA_EXCEEDED` | 402 | Quota fichiers projet atteint | `current, max, plan` |
| `LIBRARY_QUOTA_EXCEEDED` | 402 | Quota library atteint | `current, max, plan` |
| `MEMORY_QUOTA_EXCEEDED` | 402 | Quota mémoires atteint | `current, max, plan` |
| `DOCUMENTS_QUOTA_EXCEEDED` | 402 | Quota documents RAG atteint | `current, max, plan` |
| `VOICE_QUOTA_EXCEEDED` | 402 | Quota minutes voix atteint | `current, max, plan` |
| `TTS_QUOTA_EXCEEDED` | 402 | Quota chars TTS atteint | `current, max, plan` |
| `VISION_QUOTA_EXCEEDED` | 402 | Quota images Vision atteint | `current, max, plan` |
| `TASKS_QUOTA_EXCEEDED` | 402 | Quota tâches Planner atteint | `current, max, plan` |
| `LLM_QUOTA_EXCEEDED` | 402 | Cap tokens prompt 30k dépassé | `estimated_tokens, max_allowed` |

## LLM & IA

| Code | HTTP | Description | `data` |
|---|---|---|---|
| `LLM_UNAVAILABLE` | 503 | Tous les providers IA down | — |
| `CONTENT_FILTERED` | 400 | Modération métier rejette (prescription/acte légal) | — |
| `VISION_CONTENT_FILTERED` | 400 | Modération Vision rejette | — |
| `VISION_UNAVAILABLE` | 503 | Vision provider down | `provider, reason` |
| `EMBEDDINGS_UNAVAILABLE` | 503 | Embeddings provider down | `provider, reason` |
| `VOICE_UNAVAILABLE` | 503 | Voice provider down | `provider, reason` |

## Files & Uploads

| Code | HTTP | Description | `data` |
|---|---|---|---|
| `FILE_TOO_LARGE` | 413 | Fichier > cap (100MB pour /files/upload) | — |
| `FILE_TYPE_NOT_ALLOWED` | 415 | MIME hors whitelist | — |
| `FILE_CONTENT_MISMATCH` | 415 | Magic-bytes ≠ MIME annoncé (anti-smuggling) | `announced, detected` |
| `VIRUS_DETECTED` | 415 | Scan virus EICAR/ClamAV positif | `signature, scanner` |
| `IMAGE_TOO_LARGE` | 413 | Image Vision > 10MB | `current_size, max_size` |
| `AUDIO_TOO_LONG` | 413 | Audio voice > 10 min | `duration, max` |
| `STORAGE_UNAVAILABLE` | 503 | MinIO/S3 down | — |

## Resources & Validation

| Code | HTTP | Description |
|---|---|---|
| `RESOURCE_NOT_FOUND` | 404 | Ressource inexistante OU non possédée (anti-énumération) |
| `PERMISSION_DENIED` | 403 | Pas propriétaire / pas admin |
| `VALIDATION_ERROR` | 422 | Body invalide Pydantic |
| `INTERNAL_ERROR` | 500 | Erreur interne loggée, jamais détails exposés |

## Chat & Conversations

| Code | HTTP | Description |
|---|---|---|
| `DUPLICATE_REPORT` | 409 | UNIQUE composite `(user_id, message_id)` violé |
| `STREAM_CANCELLED` | 200 | SSE annulé proprement (event done reason=cancelled) |

## Planner

| Code | HTTP | Description |
|---|---|---|
| `TASK_SCHEDULE_INVALID` | 422 | Schedule type invalide ou config malformée |

## Projects

| Code | HTTP | Description |
|---|---|---|
| `PROJECT_NAME_CONFLICT` | 409 | UNIQUE partial `(user_id, LOWER(name)) WHERE deleted_at IS NULL` violé |

## RGPD & Helpdesk

| Code | HTTP | Description |
|---|---|---|
| `DELETION_REQUEST_ALREADY_EXISTS` | 409 | Une demande pending existe déjà (idempotence stricte) |
| `NO_ACTIVE_DELETION_REQUEST` | 404 | Pas de pending request à annuler |

## Paiements (Phase 11 — placeholder)

| Code | HTTP | Description |
|---|---|---|
| `PAYMENT_FAILED` | 402 | Échec paiement mobile money / carte |
| `PAYMENT_WEBHOOK_INVALID` | 400 | Signature HMAC webhook invalide |

---

## Hook escalation Crisp (Phase 18 / N4)

Quand un user **Pro** rencontre un de ces codes critiques :
- `PAYMENT_FAILED` → category `payment`, severity `high`
- `PAYMENT_WEBHOOK_INVALID` → category `payment`, severity `critical`
- `LLM_UNAVAILABLE` → category `llm_unavailable`, severity `high`

Le hook `_maybe_escalate_to_crisp` dans `core/errors/handlers.py` crée
automatiquement un ticket Crisp via `asyncio.create_task` fire-and-
forget. Voir [`docs/architecture/security-posture.md`](../architecture/security-posture.md).

---

## Catégorisation HTTP

| HTTP | Cas |
|---|---|
| **200** | Succès lecture / action idempotente |
| **201** | Création réussie (POST) |
| **204** | Suppression idempotente / mise à jour sans body |
| **400** | Erreur métier client (validation custom, captcha, content filter, token reset) |
| **401** | Non authentifié (token manquant/invalide/expiré) |
| **402** | Quota / paiement (Pro required, plan upgrade nécessaire) |
| **403** | Pas l'autorisation (admin only, Pro only) |
| **404** | Ressource introuvable (anti-énumération IDOR-safe) |
| **409** | Conflit (duplicate UNIQUE, idempotence violation) |
| **413** | Payload trop gros |
| **415** | Type média non autorisé / mismatch |
| **422** | Body Pydantic invalide |
| **429** | Rate limit / quota |
| **500** | Erreur serveur interne (log + Sentry, jamais détails exposés) |
| **503** | Service externe down (LLM, Vision, MinIO) |
