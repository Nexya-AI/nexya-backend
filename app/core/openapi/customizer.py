"""
NEXYA Backend — Customisation du schéma OpenAPI (Session O1).

Enrichit le schéma auto-généré par FastAPI pour le rendre DD-ready :
1. `info` : title/description/contact/license/termsOfService FR
2. `servers` : dev/staging/prod selon `settings.env`
3. `tags` : 18+ tags avec `description` + `externalDocs`
4. `components.securitySchemes` : `BearerAuth` (HTTP bearer JWT) +
   `PrometheusToken` (apiKey header `X-Prometheus-Token`)
5. `info.x-logo` : URL placeholder NEXYA logo (rendu par ReDoc)

Pattern de hook (FastAPI 0.100+) :

    from app.core.openapi import customize_openapi
    app.openapi = lambda: customize_openapi(app)

Le lambda permet de re-générer le schéma à chaque appel `/openapi.json`
(en dev, hot-reload routes), tout en cachant via `app.openapi_schema`
en prod (FastAPI fait le cache automatiquement).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.config import settings


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES — TAGS NEXYA (18+ catégories)
# ═══════════════════════════════════════════════════════════════════

NEXYA_TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "auth",
        "description": (
            "**Authentification & gestion du compte user.**\n\n"
            "JWT RS256 avec access token (TTL 15 min) + refresh token "
            "(TTL 30 j, rotation à chaque usage, hash SHA-256 en DB). "
            "Rate limits stricts par IP : 10 login/min, 5 register/jour. "
            "Captcha hCaptcha sur `/auth/register`. "
            "Voir aussi : `/user/profile`, `/user/password`, `/user/account`."
        ),
    },
    {
        "name": "user",
        "description": (
            "**Profil utilisateur, mot de passe, suppression compte RGPD.**\n\n"
            "Suppression RGPD = anonymisation logique (email/username/avatar/bio "
            "effacés, `is_active=false`). Hard delete différé via `/rgpd/user/"
            "account/delete-request` (workflow 2-step + 30 jours grâce)."
        ),
    },
    {
        "name": "chat",
        "description": (
            "**Chat IA streamé (SSE) + historique conversations.**\n\n"
            "`POST /chat/stream` : SSE `event: chunk / done / error` + heartbeat "
            "15 s. Annulation duale (clé Redis `chat:cancel:{session_id}` ET "
            "déconnexion HTTP). Le backend choisit le modèle selon `expert_id` "
            "(jamais le frontend). Persistance optionnelle si `conversation_id` "
            "fourni. CRUD conversations + corbeille + recherche FTS française."
        ),
    },
    {
        "name": "projects",
        "description": (
            "**Projets utilisateur (workspaces de conversations + fichiers).**\n\n"
            "Quotas Free=3 / Pro=50. Soft-delete + détache les conversations "
            "(`SET project_id=NULL`). UNIQUE partial `(user_id, LOWER(name))` "
            "pour les noms case-insensitive scope user."
        ),
    },
    {
        "name": "library",
        "description": (
            "**Bibliothèque médias (images générées + uploads user).**\n\n"
            "Quotas Free=50 / Pro=1000. Dédup SHA-256 idempotente. "
            "Presigned URLs MinIO TTL 1 h par item (jamais le storage_key brut "
            "exposé). Auto-save sur `/image/generate` (source='generated')."
        ),
    },
    {
        "name": "files",
        "description": (
            "**Upload fichiers utilisateur (PDF, DOCX, TXT, MD, images).**\n\n"
            "Pipeline 10 étapes : whitelist MIME → cap 100 MB → SHA-256 → "
            "magic-bytes anti-smuggling → dédup → scan virus (mock dev / "
            "ClamAV prod) → upload MinIO → INSERT DB → extraction texte async "
            "(pypdf / python-docx) → return upload_id. "
            "Rate limit 20 uploads/h/user."
        ),
    },
    {
        "name": "voice",
        "description": (
            "**Voix : catalogue 6 voix branded + STT/TTS Pro-only.**\n\n"
            "`GET /voice/list` accessible Free + Pro (transparence catalogue). "
            "`POST /voice/transcribe` (Whisper) + `POST /voice/speak` (TTS) "
            "= **Pro-only** (Free passe par STT/TTS natif Flutter, $0 backend)."
        ),
    },
    {
        "name": "vision",
        "description": (
            "**Analyse multimodale image+texte (Gemini Flash/Pro + GPT-4o).**\n\n"
            "Asymétrie Free/Pro par tier : Free `flash` imposé, Pro choisit "
            "`flash`/`pro`. 3 modes input mutex : `upload_id` / `library_id` / "
            "`image_base64`. Cap 10 MB / image, 4 images max par requête. "
            "Anti-prompt-injection via `VISION_SYSTEM_INSTRUCTION` préfixé."
        ),
    },
    {
        "name": "memory",
        "description": (
            "**Mémoire IA (faits durables ré-injectés dans /chat/stream).**\n\n"
            "Recherche pgvector top-K cosinus + injection auto système prompt "
            "(D3). Quotas Free=100 / Pro=10000. RGPD hard DELETE physique."
        ),
    },
    {
        "name": "rag",
        "description": (
            "**Retrieval-Augmented Generation sur documents user.**\n\n"
            "`POST /rag/query` SQL cosinus pgvector + JOIN strict `uploaded_files` "
            "(rempart IDOR cross-user) + framing anti-prompt-injection "
            "`<<<DOCUMENT EXTRACT>>>`. Rate limit 60/h."
        ),
    },
    {
        "name": "ai_models",
        "description": (
            "**Inventaire des modèles LLM disponibles + routing experts.**\n\n"
            "Aggregation runtime depuis providers initialisés. Tier flash/pro/"
            "ultra dérivé `max_context_tokens`. Mock filtré en prod."
        ),
    },
    {
        "name": "tasks",
        "description": (
            "**Planificateur de tâches IA récurrentes (Planner).**\n\n"
            "4 schedule_type : `once` / `interval_minutes` / `daily` / `weekly`. "
            "Quotas Free=3 / Pro=50. Worker arq cron `dispatch_due_tasks` "
            "chaque minute (`SELECT FOR UPDATE SKIP LOCKED`). "
            "Notifications push FCM + email fallback (F2/F3)."
        ),
    },
    {
        "name": "notifications",
        "description": (
            "**Notifications push (FCM) + email fallback + timeline in-app.**\n\n"
            "5 catégories RGPD : `tasks` / `payments` / `security` / `digest` / "
            "`product`. Préférences `push|email|both|none` par catégorie. "
            "Unsubscribe public sans auth via JWT one-click TTL 365 j. "
            "`security` non-désinscriptible (obligation légale)."
        ),
    },
    {
        "name": "feedback",
        "description": (
            "**Thumbs up/down sur les messages assistant.**\n\n"
            "UPSERT atomique `pg_insert.on_conflict_do_update` (anti-race "
            "TOCTOU). DELETE 204 idempotent anti-énumération."
        ),
    },
    {
        "name": "suggestions",
        "description": (
            "**Formulaire feedback user → équipe NEXYA.**\n\n"
            "4 types : `bug` / `feature` / `expert_domain` / `other`. "
            "Rate limit 5/jour/user. Email fail-safe via Brevo (mock dev)."
        ),
    },
    {
        "name": "rgpd",
        "description": (
            "**Conformité RGPD UE 2016/679 (Articles 7, 15, 17, 20) + AI Act "
            "EU 2024/1689 Article 13.**\n\n"
            "Export ZIP 23 fichiers (Article 20 portabilité), workflow 2-step "
            "DELETE 30j grâce (Article 17), consent log avec hash SHA-256 figé "
            "(preuve juridique anti-modification, Article 7), registre AI Act "
            "admin CSV/JSON."
        ),
    },
    {
        "name": "admin",
        "description": (
            "**Endpoints administrateurs — ACL email-list `require_admin`.**\n\n"
            "Réservés à `settings.rgpd_admin_emails`. Fail-fast au boot en "
            "prod si la liste est vide. Audit chaque accès dans `auth_events`."
        ),
    },
    {
        "name": "helpdesk",
        "description": (
            "**Support / escalation Crisp + métriques admin (Phase 18).**\n\n"
            "Hook `_maybe_escalate_to_crisp` dans `core/errors/handlers.py` "
            "fire-and-forget sur `PaymentFailedException` / `LlmUnavailableException` "
            "quand user Pro. `GET /admin/helpdesk/metrics` agrège open/in_progress/"
            "resolved counts + median age + breakdown par catégorie."
        ),
    },
    {
        "name": "observability",
        "description": (
            "**Observabilité : Prometheus metrics + status global (K1).**\n\n"
            "`/metrics` endpoint Prometheus token-protégé. "
            "`/observability/status` JSON synthèse OTel + Sentry + Prometheus."
        ),
    },
    {
        "name": "health",
        "description": (
            "**Health checks Kubernetes (liveness + readiness étendue).**\n\n"
            "`/healthz` minimal (le process répond — anti-redémarrage K8s sur "
            "DB transient). `/ready` étendu O1 : version git, latence DB/Redis, "
            "queue arq depth, dernière migration, uptime. `/version` public."
        ),
    },
]


# ═══════════════════════════════════════════════════════════════════
# CUSTOMIZER PRINCIPAL
# ═══════════════════════════════════════════════════════════════════


def customize_openapi(app: FastAPI) -> dict[str, Any]:
    """Génère un schéma OpenAPI 3.1 enrichi pour DD/Swagger production.

    Idempotent : si `app.openapi_schema` est déjà calculé, retourne le
    cache. Sinon, génère + enrichit + cache.
    """
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version="3.1.0",
        description=app.description,
        routes=app.routes,
        tags=NEXYA_TAGS_METADATA,
    )

    _apply_info(schema)
    _apply_servers(schema)
    _apply_security_schemes(schema)

    app.openapi_schema = schema
    return schema


# ═══════════════════════════════════════════════════════════════════
# HELPERS — sections du schéma
# ═══════════════════════════════════════════════════════════════════


def _apply_info(schema: dict[str, Any]) -> None:
    """Enrichit `info` avec title/description multi-lignes/contact/license/x-logo."""
    info = schema.setdefault("info", {})
    info["title"] = "NEXYA API"
    info["version"] = settings.app_version or "0.1.0"
    info["summary"] = (
        "API REST + SSE de l'assistant IA NEXYA — multi-experts, "
        "Africa-first, RGPD/AI Act compliant."
    )
    info["description"] = _NEXYA_DESCRIPTION_FR
    info["termsOfService"] = "https://nexya.ai/terms"  # placeholder
    info["contact"] = {
        "name": "Équipe NEXYA",
        "email": "support@nexya.ai",
        "url": "https://nexya.ai",
    }
    info["license"] = {
        "name": "Proprietary — Nexyalabs",
        "url": "https://nexya.ai/license",
    }
    # x-logo : extension ReDoc pour afficher le logo en haut à gauche.
    info["x-logo"] = {
        "url": "https://nexya.ai/static/logo.png",  # placeholder
        "altText": "NEXYA",
        "backgroundColor": "#0066ff",
    }


def _apply_servers(schema: dict[str, Any]) -> None:
    """Liste les serveurs disponibles pour Postman/Insomnia/clients."""
    schema["servers"] = [
        {
            "url": "http://localhost:8000",
            "description": "Développement local (Docker compose)",
        },
        {
            "url": "https://api-staging.nexya.ai",
            "description": "Staging (post-L2)",
        },
        {
            "url": "https://api.nexya.ai",
            "description": "Production",
        },
    ]


def _apply_security_schemes(schema: dict[str, Any]) -> None:
    """Déclare BearerAuth (JWT) + PrometheusToken (scrape).

    Note : on ne tag PAS chaque endpoint individuellement avec
    `security: [BearerAuth]` parce que FastAPI le fait déjà
    automatiquement via `Depends(get_current_user)` qui pose un
    `HTTPBearer` security au niveau du path. On ajoute juste la
    déclaration globale dans `components.securitySchemes` pour que
    Swagger UI affiche le bouton 🔒 « Authorize ».
    """
    components = schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "JWT RS256 access token obtenu via `POST /auth/login` ou "
            "`POST /auth/register`. Format : `Authorization: Bearer <token>`. "
            "TTL 15 min — utiliser `/auth/refresh` pour renouveler avant "
            "expiration."
        ),
    }
    schemes["PrometheusToken"] = {
        "type": "apiKey",
        "in": "header",
        "name": "X-Prometheus-Token",
        "description": (
            "Token de scrape Prometheus pour `/metrics` et `/observability/"
            "status`. Configuré via `PROMETHEUS_SCRAPE_TOKEN` env. "
            "Constant-time compare côté serveur."
        ),
    }


# ═══════════════════════════════════════════════════════════════════
# DESCRIPTION FR — multi-paragraphes
# ═══════════════════════════════════════════════════════════════════


_NEXYA_DESCRIPTION_FR = """
## 🚀 NEXYA Backend API

API REST + SSE qui alimente l'app mobile **NEXYA** (Flutter, Africa-first,
multi-experts IA). 950k utilisateurs cibles à terme.

### 🔐 Authentification

Toutes les routes protégées requièrent un **JWT RS256** dans le header :

```http
Authorization: Bearer <access_token>
```

Cycle de vie :
- `POST /auth/register` ou `POST /auth/login` → couple `(access, refresh)`.
- `access_token` TTL **15 min**.
- `refresh_token` TTL **30 jours**, rotation à chaque usage (hash SHA-256 en DB).
- `POST /auth/refresh` pour renouveler avant expiration.
- `POST /auth/logout` blackliste l'access (Redis) + révoque tous les refresh.

### 🚦 Rate limiting

| Endpoint | Limite |
|---|---|
| `POST /auth/login` | 10/min/IP |
| `POST /auth/register` | 5/min/IP + 5/jour/IP + 5/jour/device |
| `POST /chat/reports` | 10/h/user |
| `POST /suggestions` | 5/jour/user |
| `POST /vision/analyze` | 30/h/user |
| `POST /files/upload` | 20/h/user |
| `POST /rgpd/user/data-export` | 1/24h/user |
| `POST /rag/query` | 60/h/user |

Rate limit dépassé → **HTTP 429** + `data.retry_after` (secondes).

### 📦 Format de réponse uniforme

Toutes les réponses suivent le wrapper `NexyaResponse[T]` :

```json
{
  "success": true,
  "data": { ... },
  "error": null,
  "code": null
}
```

Erreur :

```json
{
  "success": false,
  "error": "Message lisible.",
  "code": "RATE_LIMIT_EXCEEDED",
  "data": { "retry_after": 1800 }
}
```

### 📋 Codes d'erreur standards

| Code | HTTP | Description |
|---|---|---|
| `AUTH_TOKEN_EXPIRED` | 401 | Access token expiré, refresh requis |
| `AUTH_TOKEN_INVALID` | 401 | Token mal formé / signature KO |
| `AUTH_REFRESH_EXPIRED` | 401 | Refresh expiré, redirect login |
| `AUTH_CREDENTIALS_INVALID` | 401 | Email/password incorrects |
| `RATE_LIMIT_EXCEEDED` | 429 | Quota journalier user atteint |
| `RATE_LIMIT_IP` | 429 | Trop de requêtes depuis cette IP |
| `RATE_LIMIT_ABUSE` | 429 | Quota anti-spam user (signaler/feedback/etc.) |
| `LLM_UNAVAILABLE` | 503 | Tous les providers IA down |
| `LLM_QUOTA_EXCEEDED` | 402 | Cap tokens prompt dépassé |
| `PLAN_REQUIRED` | 403 | Feature Pro-only |
| `RESOURCE_NOT_FOUND` | 404 | Ressource inexistante / non possédée |
| `PERMISSION_DENIED` | 403 | Pas propriétaire de la ressource |
| `VALIDATION_ERROR` | 422 | Body invalide (Pydantic) |

### 📡 Streaming SSE

`POST /chat/stream` retourne un flux SSE avec ces événements :

- `event: chunk` `data: {"delta": "..."}` — chunk de texte
- `: keepalive` — heartbeat 15 s (anti-coupure proxy 2G/3G)
- `event: done` `data: {"reason": "stop|cancelled|error"}` — fin
- `event: error` `data: {"code": "...", "message": "..."}` — erreur

Annulation : poser la clé Redis `chat:cancel:{session_id}` via
`POST /chat/stop` OU fermer la connexion HTTP.

### 🌍 Africa-first

Le backend est calibré pour :
- 2G/3G : timeouts longs, heartbeat SSE, pagination cursor-based.
- Mobile money : CinetPay / NotchPay (Phase 11, Q3 2026).
- Langues vernaculaires : Duala, Bassa, Medumba, Fulfulde (Phase H, fine-tuning Gemma).

### 🔒 Sécurité & Conformité

- **JWT RS256** (asymétrique, clé publique distribuable).
- **Captcha hCaptcha** sur `/auth/register`.
- **Rate limiting** Redis sliding window (user + IP).
- **CSP + HSTS + COOP** headers (preset prod strict).
- **RGPD** : Articles 7, 15, 17, 20 implémentés (J1).
- **AI Act EU** : Article 13 (registre des systèmes IA — applicable août 2026).

### 📊 Observabilité

- **OpenTelemetry** : traces distribuées (OTLP exporter).
- **Sentry** : exceptions + breadcrumbs.
- **Prometheus** : 14 métriques NEXYA custom + Grafana dashboards (K2).
- **Évals IA** : harness `tests/evals/` reproductible nightly (N3).
- **Load tests** : harness k6 `tests/load/` weekly (N4).

### 💬 Support

- `support@nexya.ai`
- [docs.nexya.ai](https://nexya.ai/docs) (placeholder)
- Tickets auto via Crisp pour incidents critiques Pro (Phase 18).
"""
