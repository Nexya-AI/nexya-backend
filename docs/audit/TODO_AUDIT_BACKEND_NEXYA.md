# TODO D'AUDIT — NEXYA BACKEND (ULTRA EXHAUSTIF)

> Compagnon opérationnel du `PROMPT_AUDIT_BACKEND_NEXYA.md`. À cocher au fur et à mesure.
> Chaque tâche = vérification atomique avec critère d'acceptation.
> ~700 items, ordonnés par phase A → B → C → D.
> **Objectif : zéro angle mort.**

---

## PHASE A — Cartographie & inventaire (45–60 min)

### A.1 — Inventaire fichiers Python

- [ ] `find app -type f -name "*.py" | wc -l` — confirmer 412 ± 5
- [ ] `find tests -type f -name "*.py" | wc -l` — confirmer 80+
- [ ] `find workers -type f -name "*.py" | wc -l` — confirmer 8 + worker.py + __init__
- [ ] `find migrations/versions -type f -name "*.py" | wc -l` — confirmer 19
- [ ] Compter LOC `app/` (attendu ~46k), `tests/` (~36k), `workers/` (~6k)
- [ ] Lister fichiers > 1000 lignes (signaler si > 5)
- [ ] Lister fichiers > 500 lignes (table sommaire)
- [ ] Détecter fichiers vides (sauf `__init__.py`) — finding S3 si imprévus
- [ ] Détecter fichiers `*.bak`, `*.tmp`, `*.orig` (résidus)

### A.2 — Inventaire routes FastAPI

- [ ] Introspecter `from app.main import app; for r in app.routes: print(r.path, r.methods)` (84 attendues)
- [ ] Cross-vérifier avec `docs/api/endpoints.md` — drift ?
- [ ] Vérifier l'export `docs/api/openapi.json` à jour (lancer `python -m scripts.export_openapi` ?)
- [ ] Identifier endpoints sans `Depends(get_current_user)` (publics) — liste exhaustive
- [ ] Identifier endpoints sans `response_model` — risque leak
- [ ] Identifier endpoints sans `status_code` explicite

### A.3 — Inventaire settings Pydantic

- [ ] Lister tous les `Field(...)` de `app/config.py` via AST (~150 attendus)
- [ ] Cross-vérifier avec `.env.example` (drift = finding S2)
- [ ] Identifier les `# TODO(Ivan): provisoire` (recense)
- [ ] Identifier les settings utilisés mais pas dans `Settings` (orphelins)
- [ ] Vérifier les validators (`field_validator`, `model_validator`) — couverture
- [ ] Identifier les settings sans bornes (`ge`, `le`, `min_length`, `max_length`, `pattern`)

### A.4 — Inventaire tables ORM + migrations

- [ ] Liste `Base.metadata.tables` après import de tous les models
- [ ] Cross-vérifier avec `migrations/env.py` (imports complets ?)
- [ ] Lire les 19 migrations — chaîne `down_revision` linéaire ?
- [ ] Pour chaque migration : `upgrade()` + `downgrade()` symétriques ?
- [ ] Indexes partiels (`postgresql_where=...`) cohérents avec service queries ?
- [ ] CHECK constraints SQL miroirs des Literal Pydantic ?
- [ ] FK ON DELETE policies cohérentes (CASCADE/SET NULL) ?
- [ ] Détecter les ALTER TABLE risqués (NOT NULL sans backfill, type change vector)

### A.5 — Inventaire workers arq + crons

- [ ] Liste `WorkerSettings.functions` (10 attendues)
- [ ] Liste `WorkerSettings.cron_jobs` (5 attendues — cleanup_refresh_tokens, flush_ai_sessions, dispatch_due_tasks, cleanup_old_task_results, purge_deleted_accounts)
- [ ] Vérifier `job_timeout=300`, `max_jobs=10`, `keep_result=3600` raisonnables
- [ ] Vérifier que `_on_job_start`/`_on_job_end` couvre 100% des jobs (instrumentation K1)
- [ ] Vérifier que chaque worker a son `try/except` global fail-safe

### A.6 — Inventaire dépendances pip

- [ ] Lister `pyproject.toml` deps prod (49 attendues) + dev (5 attendues)
- [ ] Vérifier ranges versions (lower bounds raisonnables, upper bounds < majeur+1)
- [ ] Identifier deps non-utilisées (grep imports vs declarations)
- [ ] Identifier imports non-déclarés (drift)
- [ ] CVE check : `pip-audit --strict --desc --skip-editable` — recenser
- [ ] Vérifier qu'il n'y a pas de deps GPL incompatibles
- [ ] `uv.lock` committé ? (probable manquant V1)

### A.7 — Inventaire features

- [ ] Lister `app/features/*` (20 attendus : ai_models, auth, chat, experts, feedback, files, helpdesk, images, library, memory, notifications, planner, projects, rag, rgpd, suggestions, vision, voice)
- [ ] Pour chacun : présence de `router.py`, `service.py`, `schemas.py`, `models.py`
- [ ] Identifier features sans router (pure helpers) ou sans models (stateless)
- [ ] Identifier features avec dépendances cross-feature (couplage)

### A.8 — Inventaire CI/CD

- [ ] Lister `.github/workflows/*` (7 attendus)
- [ ] Vérifier triggers, permissions, concurrency par workflow
- [ ] Identifier secrets référencés (`secrets.X`) → liste pour ops
- [ ] Vérifier `actions/*` versions pinned (no @main, no @latest)
- [ ] Whitelist orgs autorisées (actions, docker, astral-sh, softprops, github, dependabot, grafana)

### A.9 — Section §A du rapport

- [ ] Tableau croisé fichiers/lignes/features
- [ ] Stats globales (tables, routes, settings, workers, tests, migrations)
- [ ] Anomalies de volume signalées
- [ ] Drift documentation/code initial recensé

---

## PHASE B — Audit dimension par dimension (3–4 h)

### D1 — Architecture & design système

#### D1.1 Séparation des couches

- [ ] Pour chaque feature, vérifier `router.py` ne fait QUE déléguer (regex `await db.execute` interdit dans routers)
- [ ] Pour chaque feature, vérifier `service.py` ne fait QUE de la logique (pas d'imports `fastapi.Request`, `HTTPException`)
- [ ] Pour chaque feature, vérifier `schemas.py` ne contient QUE Pydantic
- [ ] Identifier services qui importent d'autres services (couplage cross-feature) — DAG
- [ ] Vérifier que `app/core` n'importe JAMAIS `app/features` (anti-cycle)

#### D1.2 Cohérence pattern `NexyaResponse[T]`

- [ ] Grep `response_model=` — proportion `NexyaResponse[...]` vs autres
- [ ] Identifier endpoints qui retournent `dict` brut (anti-pattern documenté CLAUDE.md §8)
- [ ] Vérifier `204 No Content` retourne `Response(status_code=204)` pas `NexyaResponse`
- [ ] Vérifier les `JSONResponse(content=...)` exotiques

#### D1.3 LlmRouter & Couche IA

- [ ] Lire `app/ai/router.py` intégralement — extensibilité d'un 6ème provider
- [ ] Vérifier `LlmRouter.resolve` lève `RouterError` correctement
- [ ] Vérifier `build_chain` filtre les non-viables avec warning
- [ ] Vérifier `build_default_router` mock-first absolument (5 providers)
- [ ] Vérifier identité usurpée Mock (name + supported_models alignés vrai)
- [ ] Confirmer le frontend ne choisit JAMAIS le modèle (grep `body.model` dans routers)
- [ ] Lire `experts.py` — 11 experts cohérents
- [ ] Vérifier `ExpertConfig.frozen=True`
- [ ] `tools_allowed=False` sur medicine/legal — vérifié
- [ ] `corpus_enabled=False` partout post-G1 cleanup — vérifié
- [ ] Disclaimers présents pour medicine/legal

#### D1.4 Couche IA — défense en profondeur

- [ ] Ordre pipeline `/chat/stream` : budget → mod OpenAI → mod regex → cap tokens → cache → stream
- [ ] Vérifier court-circuits dans bon sens
- [ ] Vérifier fail-open documenté pour modération/cache/budget
- [ ] Vérifier fail-closed documenté pour budget overflow / circuit open / quota
- [ ] CircuitBreaker : 5 échecs / 30s cooldown — calibré ?
- [ ] RetryPolicy : 3 tentatives / base 0.5s / max 5s / jitter 25 % — cohérent avec timeout SSE
- [ ] PromptCache : skip safety-critical + multi-turn — implémenté correct
- [ ] BudgetTracker : 8 méthodes — atomicité INCRBY+DECRBY validée

#### D1.5 Mock-first 8 SaaS

- [ ] Lister les 8 patterns mock-first : Brevo, hCaptcha, FCM, Vision, Voice, Embeddings, ObjectStore, VirusScanner, Crisp, C2PA
- [ ] Pour chacun : factory `get_X()` singleton lazy + warning prod si mock
- [ ] Vérifier `reset_X_for_tests()` disponible
- [ ] Vérifier no leak réelle si clé absente (ne pas crasher au boot)

#### D1.6 Conventions REST

- [ ] `POST /resource/{id}/action` pour actions non-CRUD (`/restore`, `/permanent`, `/pause`, `/resume`)
- [ ] `PATCH` pour updates partiels (vs `PUT` total)
- [ ] `DELETE` retourne 204 sans body
- [ ] `GET` listings paginés keyset (jamais OFFSET sauf page admin)
- [ ] `POST` création retourne 201

#### D1.7 API versioning

- [ ] `app/api/v1/router.py` — vide. Stratégie déclarée vs pas implémentée — finding S2
- [ ] `docs/api/versioning.md` — politique V1 unprefixed, critères bump V2 — cohérent
- [ ] Mais aucun endpoint actuellement préfixé `/v1/` — drift : finding ?

#### D1.8 Notation D1

- [ ] Synthèse points forts (mock-first, fail-safe, LlmRouter, défense en profondeur)
- [ ] Synthèse points faibles (versioning vide, certains routers > 1000 lignes, couplage features lourd)
- [ ] Note /20 + 5 raisons

### D2 — Sécurité

#### D2.1 Authentification JWT

- [ ] `app/core/auth/jwt.py` : RS256, TTL 15 min, blacklist Redis avec TTL aligné — OK
- [ ] Vérifier `decode_access_token` lève `InvalidTokenError` si type ≠ access
- [ ] `refresh.py` : SHA-256 hash, rotation, blacklist via DB (`revoked_at`) — OK
- [ ] `password_reset.py` : pwh fingerprint + TTL 15min + purpose strict — OK
- [ ] `unsubscribe_tokens.py` : TTL 365j + whitelist `cat` strict — OK
- [ ] Vérifier que `jwt_private_key` jamais loggué/scrubed
- [ ] Vérifier `jwt.PyJWKClient` n'est PAS utilisé (clé interne, pas JWKS distant)
- [ ] Algorithme RS256 hardcodé partout (jamais `algorithms=[]` qui accepte tout)

#### D2.2 Autorisation

- [ ] Grep `Depends(get_current_user)` — count
- [ ] Grep `Depends(require_pro)` — endpoints Pro-only
- [ ] Grep `Depends(require_admin)` — endpoints admin (ACL email)
- [ ] Vérifier `require_admin` fail-fast prod si liste vide (`_enforce_production_safety`)
- [ ] Pour chaque service avec `_get_owned_X` : JOIN owner check, jamais 403

#### D2.3 IDOR audit

- [ ] Lister tous les endpoints paramétrés `{id}`
- [ ] Pour chacun, vérifier owner check 404 (jamais 403)
- [ ] Vérifier que les soft-deleted ne fuient pas (clause `deleted_at IS NULL`)
- [ ] Vérifier qu'aucune jointure ne permet leak cross-user (RAG `/rag/query` JOIN strict uploaded_files validé)

#### D2.4 Input validation

- [ ] Sanitizer `clean_text` appliqué dans schemas (`field_validator`)
- [ ] Sanitizer ré-appliqué dans services pour défense en profondeur ?
- [ ] Pydantic v2 partout
- [ ] Aucun `request.body` raw lu sans validation
- [ ] `EmailStr` validation
- [ ] Bornes numériques (ge/le) sur tous les Field numeric
- [ ] Bornes string (min_length/max_length) sur tous les Field text
- [ ] Patterns regex sur identifiants (slug, expert_id, prefix)

#### D2.5 Anti-smuggling magic-bytes

- [ ] `mime_detector.py` : couvre 12 formats minimum
- [ ] OOXML discrimination via marqueur ZIP
- [ ] RIFF subtypes (WebP / WAV)
- [ ] `mimes_compatible` tolère alias légitimes (`image/jpeg ≡ image/jpg ≡ image/pjpeg`)
- [ ] Strict sinon — vérifier qu'aucun test « accepte tout » par défaut
- [ ] Couverture des formats hostiles : PDF avec macro JavaScript, ZIP nested, polyglote PNG/HTML, SVG avec <script>

#### D2.6 Anti-malware

- [ ] `virus_scanner.py` : EICAR détecté
- [ ] `NoOpVirusScanner` documenté (utilisé si `virus_scan_enabled=False`)
- [ ] `ClamAVScanner` stub `NotImplementedError` — Phase 14 documentée
- [ ] Politique fail-open `virus_status='failed'` — risque accepté à 9 M users ?
- [ ] Aucun scan async non-bloquant pour gros fichiers ? Peut bloquer event loop si > 50 MB

#### D2.7 Secrets management

- [ ] Lister tous les `settings.X_api_key`, `X_secret`, `X_password`
- [ ] Vérifier qu'aucune valeur n'est hardcodée dans le code
- [ ] `_enforce_production_safety` couvre TOUS les secrets critiques
- [ ] `JWT_PRIVATE_KEY` accepte path ou contenu PEM
- [ ] `.env.example` n'a aucune vraie valeur (juste placeholders)
- [ ] `.gitignore` exclut `.env`, `.env.prod`, `*.pem`, `*.key`

#### D2.8 Scrubber secrets

- [ ] `_scrub` couvre 9 patterns minimum (password/token/secret/api_key/apikey/authorization/private_key/webhook_secret/device_token)
- [ ] Récursif sur dict/list (testé)
- [ ] Pont Sentry `_sentry_scrub_event` couvre request.data/headers/query_string/cookies/extra/contexts/breadcrumbs
- [ ] Vérifier dans logs structlog : aucun champ password/token n'apparaît (grep)
- [ ] Vérifier `_safe_body_preview` 500 chars cap

#### D2.9 Headers HTTP O1

- [ ] 4 presets (dev/staging/prod/off) — testés
- [ ] CSP prod sans `unsafe-inline`
- [ ] HSTS preload prod (max-age=31536000; includeSubDomains; preload)
- [ ] COOP same-origin + CORP same-origin prod
- [ ] X-Frame-Options DENY
- [ ] X-Content-Type-Options nosniff
- [ ] Permissions-Policy locked
- [ ] Production safety guard refuse dev/staging en prod
- [ ] Skip CSP `/docs` `/redoc` `/openapi.json` en non-prod

#### D2.10 CORS

- [ ] `allowed_origins` jamais `*` en prod (`_enforce_production_safety` OK)
- [ ] `allow_credentials=True` validé seulement si origins explicites
- [ ] Headers `*` autorisés — risque ?

#### D2.11 Rate limiting

- [ ] 14 usages identifiés (login 10/min, register 5/min + 5/jour, forgot_password 10/h IP + 3/h email, reset 5/h, abuse 10/h user, chat 100/min, file_upload 20/h, voice_transcribe 30/h, voice_tts 60/h, vision 30/h, rag 60/h, suggestions 5/jour, unsubscribe 10/h IP, rgpd_export 1/24h)
- [ ] Sliding window Redis INCR+EXPIRE atomique
- [ ] Fail-open si Redis down — documenté
- [ ] Sentinelle privée `_ForgotPasswordEmailThrottled` non-révélée — anti-enum
- [ ] Codes d'erreur distincts (RATE_LIMIT_IP / RATE_LIMIT_ABUSE / RATE_LIMIT_EXCEEDED) cohérents

#### D2.12 SQL injection

- [ ] Grep `f"...SELECT"` ou `f"...INSERT"` ou `f"...DELETE"` ou `f"...UPDATE"` — aucun
- [ ] Tous les `text(...)` utilisent `bindparams` — confirmer
- [ ] Vérifier `chat/service.py` FTS (`q_trgm`, `q_fts` bindés)
- [ ] Vérifier `rag/service.py` cosinus (`q_vec` cast vector, `file_ids` ANY CAST uuid[])
- [ ] Vérifier `expert_corpus_service.py` cosinus + language_pair
- [ ] Vérifier `helpdesk/service.py` `percentile_cont` + filtres

#### D2.13 Webhooks paiements (Phase 11 future)

- [ ] Pattern HMAC documenté ADR/runbook
- [ ] `processed_webhooks` dedup pattern documenté
- [ ] Pas implémenté V1 — finding informationnel

#### D2.14 C2PA Content Credentials

- [ ] `app/features/images/c2pa.py` lu intégralement
- [ ] Mock-first auto si keys absentes
- [ ] Production safety guard fail-fast prod si `c2pa_enabled=True` ET keys absentes
- [ ] Hook `/image/generate` après watermark, avant INSERT Library
- [ ] Fail-safe absolu (exception → image originale + applied=False)
- [ ] AI Act Article 13 compliance documentée

#### D2.15 Risques 9 M users

- [ ] Fanout FCM : limite par notification ? Saturation Firebase quota (`https://firebase.google.com/docs/cloud-messaging/concept-options#xmppserverref` — 600k QPS) ?
- [ ] Cardinalité Prometheus : labels (5 providers × 30 models × 4 outcomes × 11 experts) = 6 600 séries — acceptable
- [ ] Mais si LlmRouter accepte modèles non-listés (warning `model_not_in_supported_set`) — explosion possible. Vérifier whitelist stricte ?
- [ ] Énumération UUID — confirmer 404 partout
- [ ] DDoS protection : rate limit IP V1 minimal — Cloudflare WAF requis L2 (documenté)

#### D2.16 OWASP Top 10 2021 mapping complet

- [ ] A01 Broken Access Control — IDOR 404 audit complet
- [ ] A02 Cryptographic Failures — JWT RS256, password bcrypt, HTTPS L2, secrets vault L2
- [ ] A03 Injection — ORM + bindparams audit
- [ ] A04 Insecure Design — défense en profondeur 4 couches
- [ ] A05 Security Misconfiguration — production safety guard fail-fast
- [ ] A06 Vulnerable Components — dependabot + pip-audit
- [ ] A07 Identification & Authentication Failures — 4 couches register, JWT rotation, captcha
- [ ] A08 Software & Data Integrity — pinned deps, GHCR signed images (futur), Dependabot
- [ ] A09 Security Logging Failures — auth_events 11 types, structlog forensic
- [ ] A10 SSRF — vérifier `httpx` n'accepte pas d'URLs user-controlled non-validées (mod OpenAI hardcoded URL ✅, mais Brevo/FCM ?)

#### D2.17 OWASP API Security Top 10 2023 mapping

- [ ] API1 BOLA (Broken Object Level Auth) — IDOR 404
- [ ] API2 Broken Authentication — couvert
- [ ] API3 Broken Object Property Level Auth — `exclude_unset` Pydantic ?
- [ ] API4 Unrestricted Resource Consumption — 14 rate limits + budget tracker
- [ ] API5 Broken Function Level Auth — require_pro / require_admin
- [ ] API6 Unrestricted Access to Sensitive Business Flows — cap chat tool rounds, cap message length
- [ ] API7 SSRF — voir D2.16
- [ ] API8 Security Misconfiguration — production safety guard
- [ ] API9 Improper Inventory Management — versioning v1 vide ?
- [ ] API10 Unsafe Consumption of APIs — providers LLM error mapping typé

#### D2.18 Notation D2

- [ ] Synthèse + note /20

### D3 — Performance, scalabilité, capacité

#### D3.1 N+1 detector

- [ ] Pour chaque service `list_*` : compter requêtes par row
- [ ] `Conversation.messages lazy='noload'` — appliqué
- [ ] `Project.files lazy='selectin' order_by uploaded_at DESC` — OK pour petites quantités
- [ ] Library : presigned URLs par item — N+1 sur HMAC local OK (pas réseau)
- [ ] Notifications listing — vérifier
- [ ] Identifier les `relationship(...)` sans `lazy=` explicite (default lazy='select' = N+1)

#### D3.2 Index DB

- [ ] Pour chaque listing, l'index partiel `(user_id, ..., DESC)` existe
- [ ] FTS `messages.search_vector` GIN — OK
- [ ] pg_trgm sur titres — OK
- [ ] HNSW pgvector mémoire (1536 dim, m=16, ef_construction=64)
- [ ] HNSW pgvector corpus (768 dim) — vide post-G1 cleanup
- [ ] Indexes UNIQUE partiels (user, sha) library + memory + uploaded_files
- [ ] Index `(user_id, content_sha256) WHERE deleted_at IS NULL` partout pour dédup
- [ ] Index `messages.search_vector` GIN — vérifier que toutes les langues sont couvertes (français)
- [ ] Index manquants identifiés ?

#### D3.3 Pool DB

- [ ] `db_pool_size=20`, `max_overflow=10`, `pool_pre_ping=True`, `pool_recycle=3600` — calibrage à 9 M users
- [ ] Calcul : 9 M × 1 % concurrent = 90 000 connexions. Avec 20 × N workers, N = 4500 workers → impossible
- [ ] Stratégie : PgBouncer (pool transactionnel), Aurora Serverless v2, Supabase Pooler
- [ ] Monitoring saturation pool : metric `nexya_db_pool_saturation` ? Non. Finding S1
- [ ] Timeout 5s sur `connect_args` — agressif sous charge

#### D3.4 Pool Redis

- [ ] `redis_max_connections=50` — calibrage 9 M users
- [ ] socket_timeout=3s
- [ ] Monitoring saturation pool : metric ? Non
- [ ] Cluster Redis nécessaire à 1 M users ?

#### D3.5 Cache prompt B2

- [ ] TTL 24h
- [ ] Économie 40-60% LLM annoncée — mesure ? Aucune metric `cache_hit_rate` directe
- [ ] Métrique `nexya_cache_operations_total` (op=get|put, outcome=hit|miss|bypass|error) — OK
- [ ] Coût mémoire Redis cumulé ? 1 entrée ≈ 5 KB × 1 M conversations cacheables = 5 GB. Acceptable.

#### D3.6 HNSW pgvector volumétrie

- [ ] Estimation : 9 M users × 100 mémoires moyennes = 900 M vectors × 1536 × 4 bytes = ~5.5 TB
- [ ] **Pas viable sans sharding partition** — finding S1 critique
- [ ] Stratégie ? Migration vers Qdrant/Weaviate/Pinecone ? Sharding par user_id ?
- [ ] À documenter : ADR future « pgvector vs Qdrant à 1 M users »

#### D3.7 SSE streaming

- [ ] Heartbeat 15s — cohérent Africa
- [ ] Sub-chunking historique (`app/main.py` legacy ?) — vérifier intégration `_run_link`
- [ ] Annulation duale (`Request.is_disconnected()` 2s + clé Redis 1s) — coûte 1 task asyncio + 1 task Redis par stream
- [ ] À 100 000 streams concurrents = 200 000 tasks watchdog. Acceptable single worker ?
- [ ] `asyncio.shield` sur finalisation — pattern correct
- [ ] Fresh `AsyncSessionLocal()` pour finalize — robuste à disconnect

#### D3.8 Workers arq

- [ ] `max_jobs=10` concurrent par process
- [ ] `job_timeout=300s` raisonnable
- [ ] Cron dispatcher chaque minute — surcharge si pas de tâches ? `SELECT FOR UPDATE SKIP LOCKED` rapide
- [ ] Calcul : 9 M users × 1 % tâches actives = 90 000 tâches/jour. Si 30 minutes moyenne entre runs = 3 000 enqueues/min, batch=50 = 60 batches/min = 1/sec. 1 worker process suffit ?
- [ ] Multi-workers : SKIP LOCKED garantit pas de double-dispatch
- [ ] Backlog : si dispatcher tombe 1h, `next_run_at` accumule — pas de rattrapage tsunami documenté
- [ ] Métrique `arq_queue_depth` (ZCARD arq:queue) exposée dans `/ready` mais pas dans `/metrics` — finding S2

#### D3.9 Storage MinIO/S3

- [ ] Presigned URLs scalables (HMAC local) — OK
- [ ] TTL 1h library, 30min files, 7j RGPD blobs — calibré ?
- [ ] Cleanup orphelins MinIO (soft-delete sans suppression synchrone) — cron Phase 12 documenté manquant
- [ ] Pas de CDN devant les blobs — Cloudflare R2 inclut, mais MinIO local non. L2 ?

#### D3.10 Bottlenecks identifiés

- [ ] Single worker uvicorn par container — `WEB_CONCURRENCY=4` env. Stratégie K8s HPA ?
- [ ] Pas de read replica DB — toutes les lectures sur le primary
- [ ] Redis singleton (pas Cluster) — limite 1 instance ~1 M ops/sec
- [ ] arq Redis backend — limite ~10 k jobs/sec vs RabbitMQ/Kafka 100 k+
- [ ] OTel SQLAlchemy `sync_engine` — pas d'async tracing fin
- [ ] Pas de queue prioritaire (cron heavy peut bloquer push notifications)

#### D3.11 SLO réalistes

- [ ] CLAUDE.md §11 cibles (auth p95 < 200ms, chat TTFB < 2s, etc.)
- [ ] Codifiés `tests/load/thresholds.json`
- [ ] Mesurés en prod ? Non V1
- [ ] Error budget burn rate alerting ? Non — finding S2

#### D3.12 Notation D3

- [ ] Synthèse + note /20

### D4 — Tests & qualité

#### D4.1 Pyramide réelle

- [ ] Compter unit (`test_X_service.py`, `test_X_router.py` mock-first) — ratio
- [ ] Compter integration (vrai DB, vrai Redis) — quasi-zéro V1 sauf migrations
- [ ] Compter E2E (TestClient FastAPI complet) — quelques uns
- [ ] Compter load (k6 — 6 scenarios)
- [ ] Compter évals IA (130 prompts × 5 catégories)
- [ ] Compter security (38 A3 hardening)
- [ ] Total 1778 tests verts — vérifier

#### D4.2 Coverage

- [ ] `coverage run -m pytest && coverage report` — % réel ?
- [ ] `fail_under=60` provisoire — réaliste ?
- [ ] Lignes mortes / branches mortes ?
- [ ] Fichiers exclus (`omit=app/main.py, app/config.py`) — justifié ?

#### D4.3 Tests de sécurité

- [ ] 38 tests A3 hardening + scrubber + JWT — couverture exhaustive ?
- [ ] Tests CSRF ? Non (CORS + JWT bearer suffit)
- [ ] Tests XSS ? Non (backend JSON-only, mais escape Jinja2 emails OK)
- [ ] Tests injection (SQL/cmd/header) ? Pas exhaustifs
- [ ] Tests fuzz JWT (claims malformés) ? Quelques tests reset_token

#### D4.4 Tests SSE & race conditions

- [ ] `test_chat_stream_persisted.py` 22 tests — couvre cancellation, fail-safe, atomicité
- [ ] Test race condition : 2 streams simultanés sur même conversation ? Non
- [ ] Test disconnect mid-stream ? Oui (mark_cancelled)
- [ ] Test placeholder orphelin (finalize crash) ? Oui (fail-safe log)

#### D4.5 Tests RGPD

- [ ] `test_data_export_service.py` 17 tests — anti-leak validés
- [ ] `test_purge_deleted_accounts_worker.py` 9 tests — workflow complet
- [ ] Test 30j grace cancel — couvert
- [ ] Test cascade DB — couvert

#### D4.6 Tests load k6

- [ ] 6 scenarios + thresholds.json — listé
- [ ] Scénario paiements manquant (Phase 11)
- [ ] Scénario fanout notifications manquant
- [ ] Scénario degradation gracieuse (LLM down) manquant
- [ ] Soak 24h non implémenté (différé V2 documenté)

#### D4.7 Tests évals IA N3

- [ ] 130 prompts × 5 catégories
- [ ] MockJudge déterministe — pipeline testable sans coût
- [ ] GeminiJudge real — nightly cron avec issue auto
- [ ] Baseline gelée + diff vs baseline
- [ ] Reproductibilité strict (temperature=0.0)
- [ ] Couverture catégories : routing / safety / format / accuracy / identity. Manquantes : multi-turn coherence, RGPD/AI Act compliance, latence p95

#### D4.8 Tests xfail / flaky

- [ ] 2 xfail strict G1 cleanup — justifiés
- [ ] `test_image_generate_failsafe_partial` flaky pré-existant — investiguer ou skipper proprement
- [ ] Autres flakys ? Identifier

#### D4.9 Manquants stratégiques

- [ ] Pas de mutation testing (`mutmut`) — gap S2
- [ ] Pas de property-based (`hypothesis`) — gap S2 surtout pour parsers
- [ ] Pas de contract testing (consumer-driven Pact entre back/front) — gap S2
- [ ] Pas de chaos testing (process kill, Redis down) — gap S2

#### D4.10 Tests Pydantic validators

- [ ] Couvrent 100 % invariants ?
- [ ] Tests bornes (min/max length, ge/le) — fréquents
- [ ] Tests model_validator (mutex sources Vision, etc.) — couverts

#### D4.11 Tests fail-safe

- [ ] Chaque `try/except: log.warning` testé qu'il NE crash PAS ?
- [ ] Tests pratiques : `test_*_failsafe_*` — recenser

#### D4.12 Notation D4

- [ ] Synthèse + note /20

### D5 — Observabilité & ops

#### D5.1 OpenTelemetry

- [ ] Auto-instrumentation 5 couches (FastAPI, SQLAlchemy via sync_engine, httpx, Redis, asgi) — installées
- [ ] Spans manuels (`ai.chat.stream`, `tools.run`, `tools.execute`, `notifications.dispatch`, `arq.{function}`)
- [ ] Sampler ParentBased(TraceIdRatioBased(0.1)) prod — calibré
- [ ] Resource attributes (service.name, service.version, deployment.environment)
- [ ] Limitation `sync_engine` — finding informationnel
- [ ] Propagation cross-service worker ↔ API — manquante documentée K1 (finding S2)
- [ ] OTLP/HTTP fail-open silencieux — OK
- [ ] `OTEL_LOG_USER_IDS=False` par défaut RGPD — OK

#### D5.2 Sentry

- [ ] DSN env-aware (vide = pas init) — OK
- [ ] 5 integrations (FastApi, SQLAlchemy, Httpx, Redis, Asyncio, Logging)
- [ ] Filtres `_should_capture` (CancelledError, NexYaException, ResourceNotFoundException) — anti-bruit
- [ ] Scrubber `_sentry_scrub_event` couvre 5 zones (request.data, headers, query_string, cookies, extra, contexts, breadcrumbs.data)
- [ ] `send_default_pii=False` — OK
- [ ] `traces_sample_rate=0.05` prod — calibré
- [ ] Profiling 0.0 V1 — OK

#### D5.3 Prometheus

- [ ] 14 métriques NEXYA custom — listées
- [ ] Buckets latence (50ms→60s) — Africa-friendly
- [ ] Endpoint `/metrics` token-protégé constant-time — OK
- [ ] Production safety fail-fast si token vide en prod — OK
- [ ] Métriques manquantes :
  - [ ] `nexya_db_pool_saturation` (open/total)
  - [ ] `nexya_redis_pool_saturation`
  - [ ] `nexya_arq_queue_depth` (juste dans /ready, pas /metrics)
  - [ ] `nexya_uploads_size_bytes_histogram`
  - [ ] `nexya_uploads_mime_total{mime}`
  - [ ] `nexya_rgpd_export_duration_seconds`
  - [ ] `nexya_rgpd_deletion_pending_count`
  - [ ] `nexya_payments_*` (Phase 11)
  - [ ] `nexya_http_requests_total{method, path, status}` (HTTP standard auto via instrumentor — vérifier)

#### D5.4 Logs structlog

- [ ] JSON prod / Console dev — OK
- [ ] Injection trace_id/span_id OTel — vérifier processor `_inject_otel_context` ordre
- [ ] PII : user_id en clair par défaut — vérifier qu'il n'y a pas de leak email/IP dans les logs
- [ ] Niveau prod INFO — OK
- [ ] Verbosité raisonnable (pas de log par chunk SSE — vérifier)

#### D5.5 Health checks

- [ ] `/healthz` liveness no-DB — OK
- [ ] `/ready` étendu (version + db latency + last_migration + redis latency + arq queue + uptime)
- [ ] `/version` public sans token — anti-leak fingerprinting validé
- [ ] `/observability/status` synthèse 3 piliers token-protégé — OK

#### D5.6 Grafana K2

- [ ] 5 dashboards JSON provisionnés
- [ ] 6 alertes Prometheus (5xx rate, chat latency, breaker open, FCM failure, arq failure, cost USD daily)
- [ ] Seuils calibrés Ivan-provisoire — recense
- [ ] AlertManager déploiement reporté L2 — finding informationnel
- [ ] `test_metric_references.py` cross-check K1↔K2 — OK

#### D5.7 Runbooks

- [ ] 3 livrés (incident-response, deployment-l2, db-restore) — relus critiques
- [ ] Manquants :
  - [ ] LLM provider down cascade
  - [ ] DB pool saturé en prod
  - [ ] Redis cluster failover
  - [ ] RGPD data breach 72h notification
  - [ ] Payment webhook failure
  - [ ] Restore from backup (testé ?)

#### D5.8 DORA metrics

- [ ] Deploy frequency : impossible V1 (déploiement manuel)
- [ ] Lead time : impossible V1
- [ ] MTTR : pas mesuré
- [ ] Change failure rate : pas mesuré
- [ ] Aucun observatoire V1 — finding S2

#### D5.9 SLO/SLI

- [ ] Cibles codifiées CLAUDE.md §11
- [ ] Pas d'error budget burn rate alerting — finding S1 critique à 9 M users

#### D5.10 Notation D5

- [ ] Synthèse + note /20

### D6 — Données & persistance

#### D6.1 Schéma DB cohérence

- [ ] 19 migrations chaînées
- [ ] UUID PK partout (UUIDMixin)
- [ ] TIMESTAMPTZ partout
- [ ] `NUMERIC(10,6)` pour coûts USD
- [ ] Naming snake_case strict
- [ ] CHECK constraints SQL miroirs Literal Pydantic — vérifier 1:1
- [ ] Drift schema ↔ code identifié

#### D6.2 Index partiels

- [ ] `WHERE deleted_at IS NULL` sur tous les listings — vérifier
- [ ] `WHERE attached_at IS NULL` pour cron uploads pending
- [ ] `WHERE chunks_indexed_at IS NULL` pour cron documents pending
- [ ] `WHERE memory_extracted_at IS NULL` pour cron memory pending
- [ ] `WHERE title_generated_at IS NULL` pour cron title pending

#### D6.3 Index UNIQUE partiels

- [ ] `(user, name) projects` — case-insensitive (LOWER) — OK
- [ ] `(user, sha) library + memory + uploaded_files` — OK
- [ ] `(file_id, chunk_index)` document_chunks — OK
- [ ] `(user_id, message_id) message_feedback` — OK
- [ ] `(user_id, type, document_version) consent_log` (actif partiel) — OK

#### D6.4 FK ON DELETE

- [ ] Conversations → Users CASCADE (purge user purge convs)
- [ ] Messages → Conversation CASCADE
- [ ] Library_items → Users CASCADE
- [ ] auth_events.user_id → SET NULL (RGPD-safe)
- [ ] source_*_id → SET NULL (références faibles)
- [ ] FCM token → SET NULL ?
- [ ] Cohérence vérifiée pour les 19 migrations

#### D6.5 Soft-delete vs hard-delete

- [ ] Soft partout
- [ ] Memory hard DELETE (RGPD Article 17 strict) — OK
- [ ] Workflow purge_deleted_accounts 30j grace — DELETE FROM users cascade SQL — OK
- [ ] Cron cleanup MinIO orphelins — Phase 12 documenté manquant — finding S2

#### D6.6 Migrations Alembic

- [ ] Chaîne `down_revision` linéaire (pas de fork) — vérifier
- [ ] `downgrade()` écrit pour les 19 — vérifier
- [ ] CI `migrations-check` upgrade head + downgrade -1 + upgrade head — V1 limité
- [ ] Migration `upgrade base` non testé V1 — finding S2 à 1 M users
- [ ] Risque migration backwards-incompatible (drop column avec données) — pratique 2-step ?

#### D6.7 pgvector dim figées

- [ ] `vector(1536)` mémoire D1 — figée DDL
- [ ] `vector(768)` corpus G1 — figée DDL (vide post-cleanup)
- [ ] Plan migration backfill documenté (drop HNSW → ALTER COLUMN → re-ingest → recreate HNSW) ~20 min
- [ ] Mais à 9 M users avec 5.5 TB vectors, ce plan ne tient pas

#### D6.8 Backup & restore

- [ ] `db-restore.md` runbook lu
- [ ] Cron backup non implémenté V1 (Phase L2 documenté)
- [ ] DR drill quarterly recommandé non testé V1
- [ ] RGPD considerations backups (export incluent backups ? Article 17 hard-delete couvre backups ?)

#### D6.9 Données sensibles RGPD

- [ ] `password_hash` redact dans data export — OK
- [ ] `device_token` mask 8 derniers chars — OK
- [ ] `ai_calls.extra` redact (peut contenir prompt) — OK
- [ ] IP anonymisée /24 IPv4 /48 IPv6 — OK
- [ ] `consent_log.document_hash` SHA-256 figé preuve juridique — OK
- [ ] Vérifier qu'aucun champ sensible n'est exposé dans les responses API

#### D6.10 Volumétrie projetée 9 M users

- [ ] `messages` : 9 M × 100 msg/user moyenne = 900 M rows. PostgreSQL OK avec partitioning par month
- [ ] `ai_calls` : 9 M × 200 calls/user/jour × 365 = 657 milliards rows/an. **IMPOSSIBLE** sans partitioning + archiving — finding S0
- [ ] `memories` : 9 M × 100 = 900 M rows. OK avec sharding pgvector
- [ ] `document_chunks` : 9 M × 5 docs × 100 chunks = 4.5 milliards rows. Critique
- [ ] `notifications` : 9 M × 10 push/jour = 90 M/jour, retention quoi ? Pas de cron purge
- [ ] Stratégie : partitioning, archiving cold storage, sharding multi-DB — pas planifié V1

#### D6.11 Replication

- [ ] Pas de read replica V1 — toutes lectures sur primary
- [ ] À 9 M users, ratio 90/10 read/write → primary saturé
- [ ] Stratégie : Aurora reader endpoint, Hetzner Managed avec replicas — Phase 19 ?

#### D6.12 Notation D6

- [ ] Synthèse + note /20

### D7 — IA & coût

#### D7.1 LlmRouter strict

- [ ] Frontend ne choisit jamais le modèle — grep `body.model` dans routers
- [ ] `expert_id` strict — fallback general si inconnu
- [ ] 11 experts cohérents
- [ ] Chaîne fallback `experts.py` cohérente
- [ ] OpenRouter pas sur safety-critical — vérifié

#### D7.2 Cost tracking

- [ ] `StreamMetrics.cost_usd` calculé par row `ai_calls`
- [ ] Grille prix `app/ai/observability.py` — à jour 2026 ? Vérifier modèles ajoutés depuis (Claude Opus 4.7, Gemini 2.5, etc.)
- [ ] Modèles fantômes (estimate_cost_usd → 0 + warning) — recense
- [ ] Précision estimation token vs facture réelle — pas mesuré V1

#### D7.3 Budget pré-flight

- [ ] 8 méthodes (chat, image, embeddings, voice_minutes, tts_chars, vision_images, ip_burst, model)
- [ ] Atomicité INCRBY+DECRBY rollback validée
- [ ] Refund vision/voice — OK
- [ ] Refund embeddings/TTS chars — manquant ? Finding S3
- [ ] Cap modèle global jamais atteint en pratique — kill-switch d'urgence

#### D7.4 Token estimator

- [ ] tiktoken o200k_base (OpenAI/o1) — OK
- [ ] tiktoken cl100k_base (Qwen) — approximation
- [ ] Heuristique chars/3.0×1.15 (Gemini/Anthropic) — précision ?
- [ ] Cap `chat_prompt_tokens_per_request_max=30k` — pré-flight 402 LLM_QUOTA_EXCEEDED
- [ ] Inclut memory_context + expert_corpus_context — OK (anti-contournement)

#### D7.5 Cache prompt B2

- [ ] SHA-256 canonique sur 6 paramètres — déterministe
- [ ] Skip safety-critical (medicine/legal) — OK
- [ ] Skip multi-turn (`_count_user_turns > 1`) — OK
- [ ] Skip troncature LENGTH — OK
- [ ] Fail-open Redis down — OK
- [ ] Hit rate observé : metric `nexya_cache_operations_total{operation=get,outcome=hit}` — Grafana panel exists ?

#### D7.6 Modération couches

- [ ] OpenAI omni-moderation (fail-open 3s) — OK
- [ ] Règles métier 7 regex FR (prescription nominative + acte juridique + jailbreaks)
- [ ] Pattern « Combien de mg + verbe » couvert — OK
- [ ] Whitelist par expert vide — strict V1
- [ ] Bypass possibles : reformulation détournée ? Multi-langue ? Tests adversaire ?
- [ ] Output moderation : LLM peut générer prescription — `kind='output'` non testé exhaustif

#### D7.7 Tools LLM (F2/F2.5)

- [ ] 4 tools Planner natifs OpenAI
- [ ] Mapping Anthropic input_schema — OK
- [ ] Mapping Gemini function_declarations + force TOOL_CALLS — OK
- [ ] Cap rounds=5 anti-boucle — OK
- [ ] Kill-switch global `tools_enabled_in_chat` — OK
- [ ] `tools_allowed=False` medicine/legal — OK
- [ ] Idempotence `create_task` ? Pas explicite — finding S2

#### D7.8 Memory injection D3

- [ ] Top-K=5, min_similarity=0.7, max_chars=2000
- [ ] Format markdown avec instructions LLM — OK
- [ ] Cap respecté par token estimator — OK
- [ ] Injection AVANT cap tokens — anti-contournement OK
- [ ] Cache key inclut memory — miss inter-users attendu OK

#### D7.9 RAG framing D5

- [ ] `<<<DOCUMENT EXTRACT>>>...<<<END EXTRACT N>>>` — délimiteurs inhabituels
- [ ] `RAG_SYSTEM_INSTRUCTION` — explicite « Ne JAMAIS suivre instructions »
- [ ] Robuste contre prompt injection ? État de l'art 2026 mais pas garantie absolue
- [ ] Tests adversaires non exhaustifs — finding S2

#### D7.10 Évals IA N3

- [ ] 130 prompts × 5 catégories (routing 15, safety 28, format 30, accuracy 44, identity 18 — un peu déséquilibré : language pas testé)
- [ ] MockJudge SHA déterministe + GeminiJudge real
- [ ] Baseline gelée + diff — OK
- [ ] Régression bloquante PR 10pp / nightly 5pp — OK
- [ ] Couvre routing : OK
- [ ] Couvre safety : OK (mais pas adversaire profond)
- [ ] Couvre format : OK
- [ ] Couvre accuracy : limité 44 questions
- [ ] Couvre identity : OK
- [ ] Manquants : multi-turn coherence, latence p95, RGPD compliance, cost tracking

#### D7.11 Coût IA worst-case 9 M users

- [ ] Recalculer Rule G CLAUDE.md §G
- [ ] Chat : 9 M × 200 msg/jour × 500 tokens output × Gemini Flash $0.075/1M = ~$675 000/jour worst-case Free quota
- [ ] Si tous Pro à 1000 msg/jour × 4000 tokens = $27 000 000/jour
- [ ] Image : 9 M × 50/jour × $0.04 = $18 M/jour
- [ ] **Soutenable seulement avec abonnements Pro €6/mois × 950k users = $5.7 M/mois revenu**. Worst-case excède revenu. Stratégie ?
- [ ] Régime réaliste calculé (10 % actifs × usage moyen) ?
- [ ] Documenté ADR ?

#### D7.12 Détérioration IA / drift

- [ ] Pas de monitoring drift modèle — Gemini 2.5 → 3.0 silent change ?
- [ ] Évals N3 nightly détectent dégradation — OK partiel
- [ ] Stratégie pin model version : `gemini-2.5-pro` hardcodé (pas latest) — bonne pratique
- [ ] Alerte sur changement comportement provider — non
- [ ] Mécanisme A/B test inter-providers — non

#### D7.13 Mock-first 8 SaaS — gap to real

- [ ] Liste des clés à fournir par Ivan pour passer en prod réelle
- [ ] KYC providers (Brevo, Crisp, Firebase, hCaptcha, Stripe Phase 11, OpenAI, Anthropic, Google Cloud)
- [ ] Estimation coût mensuel chacun à 950 k users

#### D7.14 Notation D7

- [ ] Synthèse + note /20

### D8 — Conformité légale & réglementaire

#### D8.1 RGPD Article-by-article

- [ ] Article 5 (principes) : minimisation OK, finalité OK, exactitude OK, durée OK (`retention_until` 90j default)
- [ ] Article 6 (base légale) : `legal_basis` 4 valeurs OK, mais quelle base par défaut pour chat IA ? « contract » documenté
- [ ] Article 7 (consentement) : 7 types, document_hash figé — OK
- [ ] Article 12 (information claire) : README.txt FR OK, templates emails FR OK
- [ ] Article 15 (accès) : ZIP export 23 fichiers — OK
- [ ] Article 17 (oubli) : workflow 2-step 30j — OK, mais effet sur backups ?
- [ ] Article 20 (portabilité) : JSON structuré — OK
- [ ] Article 25 (privacy by design) : cohérent
- [ ] Article 28 (DPA sous-traitants) : template placeholder — non signé V1 finding S1
- [ ] Article 32 (sécurité) : chiffrement at-rest TODO MinIO, in-transit Phase L2
- [ ] Article 33 (notification breach 72h) : runbook OK
- [ ] Article 35 (DPIA) : reportée Phase M3 — finding S2 si NEXYA = grande échelle
- [ ] Article 37 (DPO) : pas obligatoire V1 mais redevient à 9 M users — finding S1

#### D8.2 AI Act EU 2024/1689

- [ ] Article 13 (transparence) : registre `ai_calls` enrichi — OK
- [ ] Endpoint admin CSV/JSON — OK
- [ ] Classification NEXYA = limited risk — vérifier arguments
- [ ] Si NEXYA ajoute scoring/decision-support → high-risk — DPIA/audit conformité
- [ ] Disclaimer expert medicine/legal — OK
- [ ] C2PA images (E4.5) — mock-first, fail-fast prod sans clés
- [ ] Date applicabilité août 2026 — Ivan prêt ? Procédure clés X.509 documentée — OK

#### D8.3 Children data Article 8

- [ ] NEXYA collecte âge à inscription ? Recherche dans schemas auth
- [ ] Si pas vérifié âge < 16 ans → consentement parental impossible → risque
- [ ] Finding S1 si pas de gate âge

#### D8.4 Cross-border data transfers

- [ ] Sous-traitants US (OpenAI, Anthropic, Google) — DPF Privacy Shield 2.0 ratified ?
- [ ] EU (Brevo) — pas d'enjeu transfert
- [ ] Asie (Qwen DashScope International — région Singapour) — SCCs requis ?
- [ ] Documenté `docs/compliance/dpa-template.md` — partiel
- [ ] Finding S1 si pas de SCCs signés

#### D8.5 OWASP API Security mapping (couvert D2.17)

#### D8.6 PCI DSS Phase 11

- [ ] Tokenisation Stripe (jamais PAN backend) — pattern documenté
- [ ] CinetPay/NotchPay similaire ?
- [ ] Aucune impl V1 — finding informationnel

#### D8.7 Notation D8

- [ ] Synthèse + note /20

### D9 — Maintenabilité & dette technique

#### D9.1 Cyclomatic complexity

- [ ] `chat/router.py` 1107 lignes — finding S2 split recommandé
- [ ] `files/service.py` ~500 lignes
- [ ] `rgpd/data_export_service.py` ~600 lignes
- [ ] `streaming.py` ~813 lignes
- [ ] `experts.py` 464 lignes (acceptable, déclaratif)
- [ ] Routeurs/services > 500 lignes : recense

#### D9.2 Naming

- [ ] Convention snake_case/PascalCase strict — OK
- [ ] Identifiants explicites — OK la plupart
- [ ] Quelques `_X` privés legacy — recense

#### D9.3 Comments

- [ ] Ratio commentaires/code par module
- [ ] Sur-commentaire (docstrings très longs) — finding cosmétique S3
- [ ] Sous-commentaire (zones complexes sans explication) — recense
- [ ] CLAUDE.md règle « default no comments » — pas appliquée (au contraire, très commenté)

#### D9.4 Dead code

- [ ] `# TODO(Ivan): provisoire` — recense (~10 attendus pricing)
- [ ] Code commenté en bloc — recense
- [ ] Imports inutilisés (ruff F401) — actuellement 0 (CI lint passe)
- [ ] Variables inutilisées (F841 ignored ruff V1) — recense
- [ ] Fonctions privées jamais appelées — grep

#### D9.5 DRY violations

- [ ] Helpers `_encode_cursor`/`_decode_cursor` dupliqués (chat, projects, library, notifications, planner, memory) — finding S2 extraction shared
- [ ] Helpers `_anonymize_ip` dans data_export_service + suggestions/service — duplication ? Vérifier
- [ ] Helpers `_guess_extension` (mime → ext) dans library + files + voice + vision ?

#### D9.6 Magic numbers

- [ ] Constantes nommées partout `_X = N` — bonne discipline
- [ ] Hardcodes dispersés dans services ? Recense

#### D9.7 Type hints

- [ ] `Mapped[...]` ORM partout — OK
- [ ] Pydantic v2 partout — OK
- [ ] `Literal[...]` enums — OK
- [ ] Coverage type hints `mypy app/` actuellement `ignore_errors=true` — finding S2

#### D9.8 Docstrings

- [ ] Qualité top sur PromptCache, BudgetTracker, etc.
- [ ] Cohérent partout ? Échantillon

#### D9.9 Imports

- [ ] Ordre stdlib → third-party → app — vérifier ruff isort OK
- [ ] Imports lazy (`# noqa: PLC0415`) — recense, justifications acceptables ?

#### D9.10 Couplage feature → core

- [ ] Pas de cycle `app/core` → `app/features` — vérifier
- [ ] Sinon finding S0 critique

#### D9.11 Lock file

- [ ] `uv.lock` non committé — finding S2 reproducibilité
- [ ] Stratégie : commit lock, regénérer périodiquement

#### D9.12 Dependabot CVE

- [ ] Auto-merge patches/minors — OK
- [ ] Pre-existing CVEs : pypdf 5.9.0 x6, pytest 8.4.2 x1 — recense + mitigation

#### D9.13 Notation D9

- [ ] Synthèse + note /20

### D10 — CI/CD & DevEx

#### D10.1 7 workflows GHA

- [ ] ci.yml : 6 jobs — temps total ?
- [ ] release.yml : tags, GHCR, body Markdown
- [ ] codeql.yml : weekly Monday 6h UTC
- [ ] dependabot-auto-merge.yml : patches/minors auto
- [ ] evals.yml : PR mock + nightly real
- [ ] load.yml : weekly Sunday 4h UTC
- [ ] dd-exports-fresh.yml : push main check stale

#### D10.2 Permissions least-privilege

- [ ] `contents: read` par défaut
- [ ] `issues: write` ponctuel evals/load nightly
- [ ] `pull-requests: write` ponctuel evals PR
- [ ] `packages: write` release
- [ ] `security-events: write` codeql

#### D10.3 Versions actions pinned

- [ ] Whitelist 6 orgs (actions, docker, astral-sh, softprops, github, dependabot, grafana)
- [ ] Pas @main, @latest — vérifié par test

#### D10.4 Branch protection

- [ ] doc `.github/branch-protection.md` UI manuelle
- [ ] À configurer par Ivan — finding informationnel V1

#### D10.5 Pre-commit

- [ ] 7 hooks opt-in (ruff, check-yaml, large-files, merge-conflict, private-key, eof, trailing)
- [ ] `revs:` pinned strict — OK
- [ ] Documenté installation `pre-commit install` — README

#### D10.6 Makefile

- [ ] 19 targets clairs avec `## help`
- [ ] `make ci` enchaîne lint+typecheck+security+test
- [ ] `make export-dd` — DD freshness
- [ ] Manquants : `make staging-deploy`, `make rollback`, `make load-local`

#### D10.7 Docker

- [ ] Multi-stage builder + runtime — OK
- [ ] Non-root UID 1001 — OK
- [ ] Healthcheck `/healthz` 30s — OK
- [ ] WEB_CONCURRENCY=4 — paramétrable
- [ ] Image GHCR — OK
- [ ] Mais Python 3.14 (release récente) ? Stable pour FastAPI ?
- [ ] Vérifier que aioboto3 fonctionne sur Py 3.14

#### D10.8 docker-compose.prod.yml

- [ ] Stub minimal V1 (api + worker)
- [ ] Services managés externes documentés
- [ ] Reverse proxy TLS (Caddy/nginx) Phase L2
- [ ] Pas de chiffrement secrets (vault) V1 — finding S1 critique pour prod

#### D10.9 Release scripts

- [ ] `release.sh` semver bump — OK
- [ ] `rollback.sh` strict bash — OK
- [ ] `smoke_test.sh` 4 checks — OK
- [ ] Pas d'auto-deploy prod — manuel V1 documenté

#### D10.10 Évals N3 nightly

- [ ] Cron 0 3 * * * UTC
- [ ] Mock PR-blocking 10pp
- [ ] Real-judge nightly issue-auto 5pp
- [ ] Coût ~$30/mois — soutenable

#### D10.11 Load N4 weekly

- [ ] Cron Sunday 4h UTC
- [ ] 6 scenarios + thresholds.json
- [ ] Issue auto sur breach
- [ ] Pas de chaos testing — finding S2

#### D10.12 DD freshness check

- [ ] dd-exports-fresh.yml push main
- [ ] git diff --exit-code openapi.json + schema.sql
- [ ] Issue auto si stale — discipline OK

#### D10.13 Coverage gating

- [ ] fail_under=60 provisoire — V2 75 / V3 80
- [ ] Plan progression documenté

#### D10.14 Onboarding

- [ ] README.md racine 250 lignes — section « Onboarding 5 minutes »
- [ ] Test pratique : un dev junior peut atteindre `/healthz` répond en 5 min ?
- [ ] Setup local (Docker compose, alembic upgrade head, seed_dev) — testé ?

#### D10.15 Notation D10

- [ ] Synthèse + note /20

### Dt1 — Documentation & DD-readiness

- [ ] 7 architecture docs FR avec exec summary EN + Mermaid — relus critiques
- [ ] 4 compliance docs (rgpd / ai-act / security-checklist / dpa-template) — relus
- [ ] 3 API docs (endpoints / error-codes / versioning) + openapi.json — relus
- [ ] 5 ADRs format Nygard — relus
- [ ] 3 runbooks (incident-response / deployment-l2 / db-restore) — relus
- [ ] glossary.md 50+ termes — relus
- [ ] README racine 250 lignes onboarding — testé ?
- [ ] CLAUDE.md §15 journal exhaustif — cross-vérifier `git log --oneline | wc -l` vs nombre d'entrées
- [ ] Drift documentation/code par chapitre — recense
- [ ] ADRs manquantes : pgvector vs Pinecone, MinIO vs R2, Brevo vs SES, mock-first pattern, arq vs Celery, OpenRouter inclusion stratégie, OTel sync_engine workaround, hCaptcha vs Cloudflare Turnstile
- [ ] Manquants documentation : changelog API, KPIs cost dashboard, migration playbook
- [ ] Notation Dt1 + note /20

### Dt2 — Risques business & opérationnels

- [ ] Bus factor = 1 (Ivan dev solo) — risque S1
- [ ] Vendor lock-in : Gemini par défaut, Imagen unique image — risque S2
- [ ] Cost at scale facture LLM 9 M users — analyse Rule G
- [ ] Time-to-market frontend Flutter parallèle — état
- [ ] Régulation moving target (AI Act, ePrivacy, DSA/DMA) — veille active ?
- [ ] Concurrence ChatGPT/Claude/Gemini mobile — différenciation tenue par code ?
- [ ] Pas de DR multi-region V1 (Phase 19) — finding S1 selon SLA visé
- [ ] Notation Dt2 + note /20

---

## PHASE C — Synthèse transverse (45 min)

### C.1 — Top 10 Critiques (S0 + S1 bloquants L2)

- [ ] Lister les findings S0 + S1 par ordre d'impact business
- [ ] Pour chacun : 1 ligne contexte + 1 ligne recommandation + 1 ligne effort

### C.2 — Top 20 Important (S1 + S2 avant 1 M users)

- [ ] Idem
- [ ] Inclure les sujets de scale (DB partitioning, pgvector sharding, Redis cluster, read replicas)

### C.3 — Top 30 Nice-to-have (S2 + S3 sur 6 mois)

- [ ] Idem
- [ ] Inclure cosmétiques, refactors confort, ADRs manquantes

### C.4 — Matrice priorité × effort

| | Effort S (1h-1j) | Effort M (1-3j) | Effort L (3j-2sem) | Effort XL (>2sem) |
|---|---|---|---|---|
| **P0 (S0 critique)** | | | | |
| **P1 (S1 major)** | | | | |
| **P2 (S2 moderate)** | | | | |
| **P3 (S3 minor)** | | | | |

### C.5 — Roadmap 3 mois / 6 mois / 12 mois

- [ ] T+3 mois : P0 + P1 majeurs
- [ ] T+6 mois : P1 reste + P2 critiques scale
- [ ] T+12 mois : P2 + P3 + multi-region + DR

### C.6 — Risques résiduels par dimension

- [ ] Si rien fait sur D1 : conséquence à 1 M / 9 M users
- [ ] Idem D2 ... D10 + Dt1 + Dt2

### C.7 — Note globale

- [ ] Moyenne pondérée des 12 dimensions
- [ ] Pondération : sécurité ×2, perf ×2, tests ×1.5, autres ×1
- [ ] Note projetée 12 mois post-corrections P0+P1

### C.8 — Verdict 1 phrase pour DD investisseur

- [ ] « NEXYA est un backend [adjectif] pour son stade, avec [N] forces remarquables et [N] chantiers identifiés ».

---

## PHASE D — Production rapport final (30 min)

### D.1 — Format Markdown

- [ ] Table des matières clicable en haut
- [ ] Sections numérotées §A / §B / §C / §D / §Annexes
- [ ] Code blocks pour citations
- [ ] Tableaux Markdown pour notes /20
- [ ] Pas d'emojis (CLAUDE.md règle)
- [ ] Français impeccable (zéro faute)

### D.2 — Annexes

- [ ] Inventaire complet findings (CSV-like markdown)
- [ ] Méthodologie d'évaluation
- [ ] Comparatifs externes (Stripe, Linear, OpenAI, etc.)
- [ ] Limites de l'audit (pentest réel non fait, perf live non mesurée, DPA juridique pointu hors scope)

### D.3 — Sauvegarde

- [ ] `docs/audit/AUDIT_BACKEND_2026-05-01.md`
- [ ] Cible 50–80 pages

### D.4 — Message final à Ivan

- [ ] 1 paragraphe : note globale, top 3 critiques, recommandation phase 2
- [ ] Liens directs vers les sections clés du rapport

---

## CHECKLIST FINALE AVANT LIVRAISON

- [ ] Toutes les dimensions notées /20 (12 dimensions)
- [ ] Top 10 + Top 20 + Top 30 produits
- [ ] Matrice priorité × effort complétée
- [ ] Roadmap 3/6/12 mois écrite
- [ ] Note globale + projection 12 mois
- [ ] Citations `fichier:ligne` partout
- [ ] Aucune affirmation sans preuve
- [ ] Style FR impeccable
- [ ] Pas d'emojis
- [ ] Limites de l'audit clairement documentées
- [ ] Liens annexes corrects
- [ ] Cohérent avec PROMPT_AUDIT_BACKEND_NEXYA.md (chaque section attendue couverte)

---

*Fin de la TODO. Cocher chaque case au fur et à mesure. ~700 items.*
