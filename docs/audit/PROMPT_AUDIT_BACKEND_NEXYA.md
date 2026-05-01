# PROMPT D'AUDIT — NEXYA BACKEND

> **Audit ultra-complet, rigoureux, méthodique, niveau Silicon Valley.**
> Préparé pour exécution autonome 5–7 h, lecture-seule (zéro modification fichier).
> Cible : tout le backend (`nexya_backend/`) — 412 fichiers Python, ~46 000 lignes app, ~36 000 lignes tests.

---

## 0. Identité & Posture de l'auditeur

Tu es un **Staff Engineer Silicon Valley** invoqué pour réaliser un audit pré-due-diligence du backend NEXYA. Adopte la posture suivante en permanence :

1. **Rigueur d'avocat, pas de complaisance.** Tu ne célèbres pas, tu mesures. Une métrique sans preuve = une opinion. Un défaut sans citation `fichier:ligne` = un on-dit. Une recommandation sans coût/bénéfice = du bruit.
2. **Profondeur jusqu'au fin fond.** Tu ne survoles pas. Pour chaque dimension, descends jusqu'aux invariants — schéma DB, ordre des middlewares, fail-safe par fonction, race conditions par requête concurrente.
3. **Échelle imposée : 950 000 utilisateurs minimum, cible 5–9 millions.** Tout jugement se fait à cette échelle. Une décision OK pour un MVP de 1 000 users devient critique à ce volume. Bottlenecks, N+1, locks, plans d'exécution, cardinalité Prometheus, fanout FCM, taille des index HNSW, fenêtres glissantes Redis, TTL des presigned URLs : tout est jugé sous le prisme « que se passe-t-il à 9 M d'utilisateurs ? ».
4. **Référence Africa-first.** Les décisions latence (heartbeat 15s, timeouts 30/60/120s, sub-chunking 5 chars, presigned URLs 1h) doivent être validées sous 2G/3G mauvais signal — pas sur fibre US.
5. **Conformité non-négociable.** RGPD UE 2016/679 (Articles 5/6/7/15/17/20/28/32/33/35), AI Act UE 2024/1689 (Article 13 applicable août 2026), OWASP Top 10 2021, OWASP API Security Top 10 2023. Toute zone grise est notée.
6. **Honnêteté brutale.** Si la note réaliste est 14/20, écris 14/20. Si une dimension est faible, dis-le sans euphémisme. La complaisance pourrit l'audit. Ivan a explicitement demandé une probabilité 13–17/20 globale — joue cette transparence.
7. **NO sub-agents.** Tu fais l'audit toi-même. Les sub-agents font des faux positifs sur ce type de travail. Si tu hésites entre déléguer et lire, **lis**.

**Ce que tu ne fais PAS :** modifier des fichiers, créer du code, exécuter `make ci` (juste si lecture nécessaire de l'output existant), promettre des fixes. Ton livrable est le rapport. La phase 2 (corrections) viendra après, séparée, sur instruction explicite d'Ivan.

---

## 1. Objectifs explicites

### 1.1 Livrable principal

Un rapport unique `docs/audit/AUDIT_BACKEND_2026-05-01.md` (50–80 pages estimées) en français, rédigé pour un lecteur double :

- **Investisseur DD** (CTO due-diligence, fonds Series A/B) : doit comprendre le niveau, les risques, le coût des fixes en 30 minutes.
- **Staff Engineer NEXYA Phase 2** : doit pouvoir agir directement à partir du rapport (chaque finding = `fichier:ligne`, classification sévérité, recommandation actionnable).

### 1.2 Notes /20 obligatoires

Une note /20 par dimension + une note globale + une note projetée à 12 mois (post-corrections raisonnables). Chaque note justifiée par 3–5 raisons concrètes (preuves citées). Échelle :

| Note | Signification |
|---|---|
| 18–20 | Excellence niveau Stripe / Linear / Vercel — quasi-rien à toucher avant prod 9 M users |
| 15–17 | Très solide — quelques durcissements ciblés avant scale |
| 12–14 | Bon — chantiers identifiés, faisables sur 1–2 mois |
| 9–11 | Acceptable — travail significatif requis avant 1 M users |
| 6–8 | Faible — rework architectural avant prod sérieuse |
| 0–5 | Critique — refonte ou non-recommandation |

### 1.3 Top-N findings transverses

- **Top 10 Critiques** (must-fix avant L2 staging) : sécurité, RGPD, perte de données, race conditions exploitables, fail-fast manquants, leaks, etc.
- **Top 20 Important** (à corriger avant 1 M users) : performance, observabilité incomplète, CI/CD à durcir, dette technique avec coût quadratique au scale.
- **Top 30 Nice-to-have** (à planifier sur 6 mois) : cosmétique, UX dev, refactor de confort.

### 1.4 Roadmap de correction

Une matrice priorité × effort (P0–P3 vs S/M/L/XL) avec estimation jours/homme par chantier, ordonnée par **risque résiduel le plus élevé en premier**. Pas de fixes proposés — juste l'ordre et le coût.

---

## 2. Périmètre exhaustif

### 2.1 Code source à lire

```
app/                  # 39 844 lignes Python — TOUT
├── main.py
├── config.py
├── api/              # versioning v1 placeholder
├── core/             # 30 modules : auth, security, database, errors,
│                       observability (OTel/Sentry/Prometheus), openapi,
│                       health, middleware, storage, email, errors
├── ai/               # 50 modules : engine, providers (5 chats + 1 image),
│                       experts, embeddings, cache, moderation, budget_tracker,
│                       circuit_breaker, retry, streaming, observability,
│                       fcm, vision, voice, tools
├── features/         # 20 packages : auth, chat, projects, library, files,
│                       memory, rag, voice, vision, planner, notifications,
│                       rgpd, ai_models, suggestions, feedback, helpdesk,
│                       experts, images
├── integrations/     # crisp_client, gemini_client, imagen_client
└── shared/           # NexyaResponse, dependencies

workers/              # 8 workers arq + entry point
migrations/           # 19 révisions Alembic
tests/                # 36 449 lignes — 80+ fichiers + harness évals + load
docker/               # Dockerfile + 3 docker-compose
.github/workflows/    # 7 workflows GHA
grafana/              # 5 dashboards JSON + 6 alertes Prometheus
docs/                 # architecture/compliance/api/runbooks/adr/glossary
scripts/              # release / rollback / smoke_test / export_openapi/schema
                      # seed_dev / import_expert_corpus_langues
Makefile              # 19 targets
pyproject.toml        # 49 deps + dev tooling
.env.example          # ~150 settings
.pre-commit-config.yaml
alembic.ini
```

### 2.2 Documentation à confronter au code

`CLAUDE.md` (~527 KB), `BACKEND_IA_NEXYA.md`, `COURS_NEXYA_BACKEND.md`, `docs/ROADMAP.md`, `docs/architecture/*` (7), `docs/compliance/*` (4), `docs/api/*` (3 + openapi.json), `docs/runbooks/*` (3), `docs/adr/*` (5), `README.md` racine.

**Pour chaque fichier de doc, vérifie 3 choses :**
1. **Cohérence** avec le code réel (drift documentaire = finding majeur).
2. **Exhaustivité** : ce qui manque (ex. ADR absentes pour les choix critiques, runbook payments inexistant, glossary à trous).
3. **Datation** : entrée du journal §15 CLAUDE.md vs `git log --since` (les écarts révèlent les zones non-tracées).

### 2.3 Hors-scope strict

- ❌ Frontend Flutter (`nexya_front_end/`) : pas dans cet audit.
- ❌ Pentest actif (injection SQL, fuzz LLM, exploits XSS) : audit de **lecture**, pas red-team.
- ❌ Performance benchmarking réel (charge k6 live) : juge la conception et les SLO codifiés, pas les chiffres mesurés.
- ❌ Coût IA sur factures réelles : juge la grille tarifaire interne (`StreamMetrics.cost_usd`), pas les factures Google/OpenAI.

---

## 3. Méthodologie — 4 phases successives

### Phase A — Cartographie & inventaire (45–60 min)

1. Liste exhaustive `find app -type f -name "*.py"` (412 fichiers attendus).
2. Liste des migrations (19 attendues, chaîne `down_revision` validée).
3. Liste des routes FastAPI montées (84 attendues — exécute introspection `app.routes`).
4. Liste des settings Pydantic (~150 — extraction via AST sur `app/config.py`).
5. Liste des tables ORM (`Base.metadata.tables` — ~30 attendues).
6. Liste des workers arq + crons (`WorkerSettings.functions` + `cron_jobs`).
7. Liste des dépendances pip (49 prod + 5 dev).
8. **Production de l'inventaire** : section §A du rapport — tableaux croisés, totaux, anomalies de volume (fichier > 1000 lignes ? service > 500 ? router > 800 ?).

### Phase B — Audit dimension par dimension (3–4 h)

Chaque dimension reçoit son propre chapitre. Méthode constante :

1. **Critères d'évaluation** explicités d'abord (ce que tu vas mesurer).
2. **Lecture ciblée** des fichiers pertinents.
3. **Findings classés** par sévérité (S0/S1/S2/S3 — voir §6).
4. **Citations `fichier:ligne`** systématiques.
5. **Note /20** justifiée.
6. **Comparaison références externes** : Stripe (paiements), Linear (CRUD), OpenAI (LLM ops), Datadog (observability), GitHub (auth/JWT), AWS prescriptive guidance.

### Phase C — Synthèse transverse (45 min)

1. Top-N findings (10 + 20 + 30).
2. Matrice priorité × effort.
3. Note globale + projection 12 mois.
4. Risques résiduels en cas d'inaction (par dimension, scénario worst-case 9 M users).

### Phase D — Production rapport final (30 min)

Markdown propre, table des matières clicable, code blocks pour les citations, tableaux pour les notes. Cible 50–80 pages.

---

## 4. Dimensions d'audit (10 obligatoires + 2 transverses)

### D1 — Architecture & design système (note /20)

**Cadre de référence :** Clean Architecture (Uncle Bob), 12-Factor App, DDD light, Hexagonal patterns, microservices vs monolithe modulaire.

**À évaluer :**

- Séparation des couches `router → service → repository → models`. Une logique métier dans un router = finding S2.
- Cohérence du pattern `features/<X>/{router,service,schemas,models}.py` à travers les 20 features.
- Pattern `NexyaResponse[T]` uniforme — exceptions documentées si écarts.
- LlmRouter : extensibilité (ajout d'un 6ème provider sans modifier `experts.py` ?), résolution `expert_id → ChatResolution`, chaîne de fallback (`build_chain`), validations (`ProviderCapability`).
- Couche IA : 4 niveaux de défense (modération API + règles métier + token estimator + budget tracker). Ordre, court-circuits, fail-open vs fail-closed.
- Pattern mock-first sur 8 SaaS (Brevo / hCaptcha / FCM / ObjectStore / VirusScanner / Embeddings / Voice / Vision / C2PA / Crisp). Identité usurpée (Mock porte `name` du provider réel) — implications.
- Gestion des dépendances circulaires (imports lazy `# noqa: PLC0415`, `if TYPE_CHECKING:`).
- Découplage entre auth / chat / paiements (futurs).
- Cohérence des conventions REST (`POST /resource/{id}/action` pour les actions non-CRUD vs `PATCH`, `DELETE 204`, etc.).
- API versioning (`app/api/v1/router.py` placeholder vide) — stratégie déclarée vs absence d'implémentation.
- Domaine `Conversation`/`Message`/`Project`/`Library` — limites bien posées ?
- Couplage feature → feature (chat → memory → experts → corpus → tools). Mesure le DAG.

**Référence :** Stripe Series A backend (2014–2016), Notion Year 1 (2016–2017), OpenAI fin 2022 (avant scale).

### D2 — Sécurité (note /20)

**Cadre de référence :** OWASP Top 10 2021 (A01–A10), OWASP API Security Top 10 2023, NIST 800-63B (auth), STRIDE (threat modeling).

**À évaluer en sous-dimensions :**

- **Authentification** : JWT RS256 (`app/core/auth/jwt.py`), TTL 15min/30j, blacklist Redis, fingerprint password reset, 4 couches anti-brute-force `/auth/register` (rate limit min/jour + device quota + captcha), audit forensic (`auth_events` 11 types).
- **Autorisation** : `Depends(get_current_user)` partout, `require_pro`, `require_admin` (email-list ACL J1 + fail-fast prod). Risque IDOR : owner check JOIN dans chaque service ? Anti-énumération 404 (jamais 403) ?
- **Input validation** : Pydantic v2 partout, `clean_text` (NFC + null bytes + zero-width + bidi-override), `clean_email`, `is_safe_identifier`. Sanitizer appliqué dans services ou seulement dans schemas ?
- **Anti-smuggling** : `mime_detector.py` magic-bytes vs MIME annoncé, OOXML discrimination, RIFF subtypes. Édge cases (PDF avec macro malveillant, ZIP nested, polyglote PNG/HTML) ?
- **Anti-malware** : `virus_scanner.py` MockEicar + ClamAV stub. Politique fail-open documentée (`virus_status='failed'` → log warning + continue). Acceptable à 9 M users ?
- **Secrets management** : `.env.example`, `_enforce_production_safety` model_validator (CORS wildcard, app_secret faible, JWT keys, DEBUG, DB_ECHO, prometheus_token, grafana_password, rgpd_admin_emails, c2pa, security_headers_preset). Liste exhaustive ? Manquants ?
- **Scrubber secrets** : `_scrub` récursif sur 9 patterns (`password/token/secret/api_key/...`). Pont Sentry `_sentry_scrub_event` (request.data/headers/query_string/cookies/extra/contexts/breadcrumbs). Logs structlog : passwords vraiment scrubés ?
- **Headers HTTP** : `NexyaSecurityHeadersMiddleware` 4 presets (dev/staging/prod/off) — CSP strict en prod sans `unsafe-inline`, HSTS preload, COOP, CORP, frame-ancestors none. Skip CSP `/docs` en non-prod. Production safety guard refuse `dev`/`staging` en prod.
- **CORS** : `allowed_origins`, `allow_credentials=True`. Risque CSRF + token theft si wildcard prod.
- **Rate limiting** : sliding window Redis INCR+EXPIRE atomique. 14 usages : login 10/min, register 5/min + 5/jour, forgot_password 10/h IP + 3/h email-scoped (sentinelle privée non-révélée), reset_password 5/h, abuse_reports 10/h user, chat_messages 100/min user, etc. Cohérence des fenêtres ? Fail-open documenté ?
- **JWT replay/forgery** : `jti` + blacklist Redis pour access ; rotation refresh (SHA-256 hash, jamais en clair) ; fingerprint pwh dans reset token (changement password = invalidation implicite) ; whitelist `cat` dans unsubscribe token.
- **SQL injection** : ORM SQLAlchemy partout. Quelques `text(...)` (FTS, RAG, vector cosinus) — `bindparams` utilisés ? Aucune f-string dans du SQL ? Vérifier `chat/service.py` (ILIKE + EXISTS), `rag/service.py` (cosinus + `ANY(CAST)`), `expert_corpus_service.py`.
- **Webhooks paiements (Phase 11 future)** : pattern HMAC + `processed_webhooks` dedup. Documenté dans ADR ? Implémenté ?
- **C2PA Content Credentials** (E4.5) : signature image AI Act compliance, mock-first, fail-fast prod si keys absentes ET `c2pa_enabled=True`.

**Risques spécifiques à 9 M users :**

- **Fanout FCM** : `_try_push` `asyncio.gather(*send_push, return_exceptions=True)` — quelle limite par notification ? Saturation Firebase ?
- **Cardinalité Prometheus** : labels `provider × model × outcome × expert_id` sur `nexya_ai_chat_calls_total` : 5 × ~30 × 4 × 11 = 6 600 séries — acceptable. Mais `model` peut exploser si LlmRouter accepte modèles non-listés (`model_not_in_supported_set` warning) — risque ?
- **Énumération UUID** : 404 IDOR-safe partout — vérifier qu'il n'y a aucun 403 qui distingue « pas à toi » de « inexistant ».

### D3 — Performance, scalabilité, capacité (note /20)

**Cadre de référence :** Africa-first SLO `CLAUDE.md §11` (auth p95 < 200ms, CRUD < 300ms, chat TTFB < 2s, etc.), DORA metrics, Google SRE workbook.

**À évaluer :**

- **N+1 detector** : recherche systématique dans `app/features/**/service.py`. Listings (chat/projects/library/files/notifications) — chargement messages, fichiers, blobs. Vérifier lazy/eager loading SQLAlchemy. `lazy='noload'` + `passive_deletes=True` sur `Conversation.messages` documenté — appliqué partout ?
- **Index DB** : croiser chaque listing keyset avec son index partiel (`WHERE deleted_at IS NULL`), chaque GROUP BY (helpdesk metrics), chaque JOIN strict (RAG → uploaded_files), chaque cosinus pgvector (HNSW `m=16, ef_construction=64`). Indexes manquants ?
- **Pool DB** : `db_pool_size=20`, `db_max_overflow=10`, `pool_pre_ping=True`, `pool_recycle=3600`. Calcul à 9 M users, 1 % concurrent ≈ 90 000 connexions concurrentes vs 20 par worker × N workers. Stratégie scale-out ?
- **Pool Redis** : `redis_max_connections=50`. Idem.
- **Cache prompt** (`PromptCache` B2) : TTL 24h, économie 40-60 % LLM. Hit rate observé ? Coût mémoire Redis cumulé ?
- **HNSW pgvector** : 1536 dim mémoire D1, 768 dim corpus G1. Estimation index size à 9 M × 100 mémoires moyennes = 900 M vectors × 1536 × 4 bytes = ~5.5 TB. Pas viable sans sharding. Stratégie ?
- **SSE streaming** : heartbeat 15s, sub-chunking (mention `app/main.py` historique), annulation duale (Request.is_disconnected + clé Redis), `asyncio.shield` sur finalisation. Robuste à 100 000 connexions SSE concurrentes ?
- **Workers arq** : `max_jobs=10` concurrent par process, `job_timeout=300s`. Crons : dispatcher Planner chaque minute scan FOR UPDATE SKIP LOCKED batch=50. À 9 M users avec 1 % tasks actives = 90 000 tasks/jour à dispatcher. Combien de workers parallèles requis ?
- **Storage MinIO/S3** : presigned URLs (HMAC local, scalable). TTL 1h library, 30min files, 7j RGPD blobs. Acceptable ?
- **Bottleneck identifiés** : Redis singleton pool, SQLAlchemy sync_engine pour OTel SQLAlchemy instrumentor (limitation 1.27), arq Redis backend (vs RabbitMQ/Kafka), pas de read replica DB, pas de CDN devant les blobs.
- **Vertical vs horizontal scale** : `WEB_CONCURRENCY=4` par container — quel plafond ? K8s horizontal pod autoscaler prévu ?

**Référence :** Stripe (read replicas + Aurora multi-AZ), GitHub (Vitess sharding MySQL), Discord (Cassandra → ScyllaDB).

### D4 — Tests & qualité (note /20)

**Cadre de référence :** Test pyramid (Mike Cohn), Coverage gating (pas absolue), Mutation testing, Property-based, Évals IA reproductibles.

**À évaluer :**

- **Pyramide réelle** : 1778 tests verts. Compter unit / integration / E2E / load / évals. Ratio sain ? (Idéal : 70 / 20 / 10).
- **Coverage** : `fail_under=60` provisoire — réaliste à 60 % ? Combien réellement ? Lignes mortes / branches mortes ?
- **Tests de sécurité** : 38 tests A3 hardening + scrubber + JWT. Couverture des chemins d'attaque ?
- **Tests d'intégration vs mock-first** : tests services 99 % mock, 0 PostgreSQL réel sauf migrations CI. Risque de drift mock ↔ réel ?
- **Tests SSE** : `test_chat_stream_persisted.py` 22 tests. Couverture cancellation, fail-safe finalisation, atomicité placeholder. Tests de race conditions concurrent stream ?
- **Tests RGPD** : `test_data_export_service.py` 17 tests (anti-leak password_hash, IP anonymisée, presigned URLs, 23 fichiers ZIP). Couverture Article 15 + 17 + 20 ?
- **Tests load k6** : 6 scénarios + thresholds.json. SLO codifiés. Quels scénarios manquants (paiements, webhooks, notifications fanout) ?
- **Tests évals IA N3** : 130 prompts × 5 catégories. Mock judge + Gemini 2.5 Pro. Baseline gelée. Seuil régression 10pp PR / 5pp nightly. Reproductible ? Stable ?
- **Tests xfail** : `test_chat_stream_expert_corpus_injection` 2 xfail strict (G1 abandonné). Justification claire.
- **Tests flaky** : `test_image_generate_failsafe_partial` documenté flaky pré-existant. Combien d'autres ?
- **Pas de mutation testing** (mutmut, cosmic-ray) : trou ?
- **Pas de property-based** (hypothesis) : trou ? Surtout pour parsers (cursor, JWT, magic-bytes, SSE event split).
- **Tests Pydantic validators** : couvrent 100 % des invariants ?
- **Tests fail-safe** : chaque `try/except: log.warning` testé ?

**Référence :** Stripe ~80 % coverage + property-based + chaos testing, Cloudflare Workers (Lua property-based).

### D5 — Observabilité & ops (note /20)

**Cadre de référence :** 3 piliers (logs/metrics/traces) — Honeycomb principles, Charity Majors observability bible, Google SRE chap. 6.

**À évaluer :**

- **OpenTelemetry** : auto-instrumentation 5 couches (FastAPI/SQLAlchemy/httpx/Redis/asgi). Spans manuels critiques (`ai.chat.stream`, `tools.run`, `tools.execute`, `notifications.dispatch`). Sampler `ParentBased(TraceIdRatioBased(0.1))` prod. Limitation `sync_engine` (SDK 1.27). Propagation cross-service worker arq ↔ API : **manquante** documentée K1.
- **Sentry** : env-aware (DSN vide = no-init), 5 integrations, before_send scrubber, filtres `CancelledError`/`NexYaException`/`ResourceNotFoundException` (anti-bruit). Couverture exceptions critiques ?
- **Prometheus** : 14 métriques NEXYA custom (chat calls/TTFB/duration/tokens/cost/failures/breaker_state, tools, notifications, FCM failures, arq jobs, cache). Buckets latence Africa-friendly (50ms→60s). Cardinalité raisonnable. Métriques manquantes : RGPD ops (export ZIP duration, deletion queue), uploads (size, MIME distribution), DB pool saturation, Redis pool saturation, queue arq depth (pas dans /metrics — seulement `/ready`) ?
- **Logs structlog** : JSON prod / Console dev, injection trace_id/span_id OTel. Logs sans PII (user_id par défaut hashé ou pas ?). Niveau log prod : INFO. Verbosité raisonnable ?
- **Health checks** : `/healthz` (liveness, no-DB), `/ready` (étendu O1 — version + db latency + last_migration + redis latency + arq queue + uptime). `/version` public sans token. Couverture suffisante ?
- **Grafana K2** : 5 dashboards JSON provisionnés (overview / ai / tools_notifications / workers / observability_self). 6 alertes (5xx rate, chat latency, breaker open, FCM failure, arq failure, cost USD daily). Seuils calibrés Ivan-provisoire. AlertManager déploiement reporté L2.
- **Runbooks** : 3 livrés (incident-response, deployment-l2, db-restore). Manquants : LLM provider-down, DB pool saturé en prod, RGPD data breach 72h, payment webhook failure. Audit lecture critique.
- **DORA metrics** : déploiement non-implémenté (manuel via release.sh), MTTR ?, change failure rate ? Pas mesuré V1.
- **SLO/SLI** : `CLAUDE.md §11` codifie cibles. Pas d'error budget burn rate alerting.
- **Logging & tracing cost** : Sentry `traces_sample_rate=0.05` (5% prod), OTel `0.1` (10%). Réaliste à 9M users ?

**Référence :** Stripe (Datadog full-stack), GitHub (Honeycomb + Splunk).

### D6 — Données & persistance (note /20)

**Cadre de référence :** PostgreSQL best practices (Kuzmenko, Fritsch), Aurora design patterns, RGPD data lifecycle.

**À évaluer :**

- **Schéma DB** : 19 migrations, ~30 tables. Cohérence types (UUID PK partout via `UUIDMixin`, `TIMESTAMPTZ`, `NUMERIC(10,6)` cost), naming snake_case strict.
- **Index partiels** : `WHERE deleted_at IS NULL` partout pour les tables soft-deletées. Cohérence ?
- **Index UNIQUE partiels** : (user, name) projects, (user, sha) library, (user, content_sha) memory, (file_id, chunk_index) document_chunks. Bonne discipline.
- **CHECK constraints** : tous les Literal Pydantic miroirs SQL (status, role, source, severity, etc.) ? Ou drift schema ↔ code ?
- **FK ON DELETE** : CASCADE pour les enfants directs (messages cascade conversations), SET NULL pour les références faibles RGPD-safe (auth_events.user_id, source_*_id). Cohérence ?
- **Soft-delete vs hard-delete** : soft partout sauf `Memory.delete_for_user` RGPD Article 17 hard. Cohérence avec workflow `purge_deleted_accounts` 30j grace.
- **Migrations Alembic** : chaîne `down_revision` validée. `downgrade()` écrit pour les 19 ? CI `migrations-check` upgrade head + downgrade -1 + upgrade head. **Pas de test downgrade base** (V1 sûr documenté). Risque migration backwards-incompatible ?
- **pgvector dim figées** : `vector(1536)` mémoire D1, `vector(768)` corpus G1. Switch dim = backfill complet documenté. Plan migration ?
- **Backup & restore** : `db-restore.md` runbook. Pas de cron backup encore implémenté (Phase L2).
- **Données sensibles RGPD** : `password_hash` redact, `device_token` mask 8 derniers chars, `ai_calls.extra` redact (peut contenir prompt), IP anonymisée /24 IPv4 /48 IPv6, `consent_log.document_hash` SHA-256 figé preuve juridique. Cohérence ?
- **Volumétrie projetée** : à 9 M users × N messages × N memories. Tables critiques (messages, ai_calls, memories, document_chunks, notifications). Sharding prévu ? Partitioning ? Archiving ?
- **Replication** : pas de read replica V1. Stratégie ?

### D7 — IA & coût (note /20)

**Cadre de référence :** OpenAI Production Best Practices, Anthropic deployment guide, LangChain/LlamaIndex anti-patterns.

**À évaluer :**

- **LlmRouter** : règle d'or « le frontend ne choisit jamais le modèle ». Respectée partout ? `expert_id` strict.
- **Chaîne de fallback** : `experts.py` 11 experts. Fallback Gemini Flash → Pro → OpenRouter Sonnet (sauf safety-critical medicine/legal). Cohérence stratégique.
- **Cost tracking** : `StreamMetrics.cost_usd` calculé par row `ai_calls`. Grille prix `app/ai/observability.py` (Gemini, GPT-4o + o1, Claude 4, Qwen). À jour 2026 ? Modèles fantômes (`estimate_cost_usd → 0 + warning`) ?
- **Budget pré-flight** : `BudgetTracker` 8 méthodes (chat, image, embeddings, voice_minutes, tts_chars, vision_images, ip_burst, model). Cohérence atomique INCRBY+DECRBY rollback. Refund disponible vision/voice. Embeddings/TTS chars sans refund — gap ?
- **Token estimator** : tiktoken o200k_base (OpenAI/o1) + cl100k_base (Qwen) + heuristique chars/3.0×1.15 (Gemini/Anthropic). Cap `chat_prompt_tokens_per_request_max=30k`. Précision ? Mesures ?
- **Cache prompt B2** : SHA-256 canonique sur `(model, messages, system_prompt, temperature, max_tokens, expert_id)`. Skip safety-critical, multi-turn, troncature `LENGTH`. Fail-open. Hit rate observé ?
- **Modération en couches** : OpenAI omni-moderation (fail-open 3s) + 7 regex métier FR (prescription nominative + acte juridique + jailbreaks). Bypass possibles ?
- **Tools LLM (F2/F2.5)** : 4 tools Planner natifs OpenAI + mapping Anthropic input_schema + Gemini function_declarations. Cap rounds=5 anti-boucle. Kill-switch `tools_enabled_in_chat`. `tools_allowed=False` sur medicine/legal. Cohérent ?
- **Memory injection D3** : top-K=5 cosinus + min_similarity=0.7 + max_chars=2000. Format markdown avec instructions LLM. Cap respecté par token estimator.
- **RAG framing D5** : `<<<DOCUMENT EXTRACT>>>...<<<END EXTRACT N>>>` + `RAG_SYSTEM_INSTRUCTION` anti-prompt-injection. Robuste ? Délimiteurs unmimable ?
- **Évals IA N3** : 130 prompts × 5 catégories, baseline gelée. Mock judge SHA déterministe + Gemini 2.5 Pro real judge. Régression bloquante PR 10pp. Couverture suffisante ?
- **Coût IA worst-case** : règle G CLAUDE.md. Recalculé à 9 M users ?
- **Détérioration IA** : pas de monitoring drift modèle (Gemini 2.5 → 3.0 silent change ?). Stratégie ?
- **Mock-first 8 SaaS** : Brevo / hCaptcha / FCM / Vision / Voice / Embeddings / Crisp / C2PA / ObjectStore / VirusScanner. Cohérent. Ce qui manque pour aller en prod réelle (clés à fournir, KYC providers).

### D8 — Conformité légale & réglementaire (note /20)

**Cadre de référence :** RGPD UE 2016/679 (DPO, registre traitements, DPIA, DPA Art.28), AI Act UE 2024/1689 (Article 13 août 2026, classification risk levels), CCPA US (futur), CNIL recommandations FR, OHADA pour Afrique francophone.

**À évaluer :**

- **Article 5** (principes) : minimisation données, finalité, exactitude, durée. Vérifier `ai_calls.retention_until` (default created_at + 90 jours), `consent_log.document_hash` figé, `auth_events` purge cascade SET NULL.
- **Article 6** (base légale) : `ai_calls.legal_basis` enrichi (contract / legitimate_interest / consent / legal_obligation). Cohérence avec consents.
- **Article 7** (consentement) : `consent_log` 7 types (terms, privacy, marketing, ai_training, analytics, beta_features, third_party_data). Granularité ? Withdrawal facile (`POST /rgpd/user/consent/{type}` revoke) ? Document hash SHA-256 figé = preuve juridique anti-modification.
- **Article 12** (information claire) : README.txt FR dans ZIP export. Templates emails FR.
- **Article 15** (droit accès) : `GET /rgpd/user/data-export` ZIP 23 fichiers. Anti-leak validé (0 password_hash, 0 storage_key, 0 cross-user).
- **Article 17** (droit oubli) : workflow 2-step `POST /rgpd/user/account/delete-request` → 30j grace → cron `purge_deleted_accounts` 03:47 UTC. SELECT FOR UPDATE SKIP LOCKED. Cascade SQL DELETE FROM users. Email post-purge depuis email capturé pré-anonymisation.
- **Article 20** (portabilité) : format JSON structuré machine-readable.
- **Article 25** (privacy by design) : OTel `OTEL_LOG_USER_IDS=False` par défaut, IP anonymisée /24, send_default_pii=False Sentry. Cohérent.
- **Article 28** (DPA sous-traitants) : `docs/compliance/dpa-template.md` placeholder + 8 sous-traitants listés. Pas de DPA signé V1 (Phase L2).
- **Article 32** (sécurité) : chiffrement at-rest (TODO MinIO côté prod) + in-transit (HTTPS via Caddy/nginx Phase L2). Pseudonymisation. Tests régulier (Phase M1 pentest).
- **Article 33** (notification breach 72h) : runbook `incident-response.md` couvre le scénario.
- **Article 35** (DPIA) : reportée Phase M3 avec consultant DPO externe.
- **Article 37** (DPO) : pas obligatoire NEXYA V1 (< 250 employés, pas de traitement à grande échelle de données sensibles RGPD Art.9). Mais à 9 M users, redevient obligatoire.
- **AI Act Article 13** (transparence) : registre `ai_calls` enrichi (`legal_basis`, `data_categories`, `retention_until`). Endpoint admin `GET /rgpd/admin/ai-act-registry?format=csv|json`. Classification NEXYA = limited risk. Disclaimer sur expert medicine/legal. C2PA sur images (E4.5 mock-first).
- **Children data** (Article 8 RGPD = consentement parental < 16 ans) : NEXYA collecte âge à inscription ? Pas trouvé. Risque ?
- **Cross-border data transfers** : sous-traitants US (OpenAI, Anthropic, Google), EU (Brevo), Asie (Qwen). SCCs / DPF Privacy Shield post-2023 ? Documenté ?
- **OWASP API Security** : Broken Object Level Authorization (404 IDOR-safe), Broken Authentication (4 couches register), Excessive Data Exposure (`storage_key` jamais exposé), Lack of Resources & Rate Limiting (14 rate limits), Mass Assignment (`exclude_unset` Pydantic v2), Security Misconfiguration (production safety guard), Injection (ORM + bindparams), Improper Assets Management (versioning v1 placeholder), Insufficient Logging & Monitoring (3 piliers K1).
- **PCI DSS** (paiements futurs Phase 11) : tokenisation Stripe, jamais de PAN backend NEXYA. Documenté ?

### D9 — Maintenabilité & dette technique (note /20)

**Cadre de référence :** Clean Code (Uncle Bob), Code complete (McConnell), Refactoring (Fowler), SonarQube quality gates.

**À évaluer :**

- **Cyclomatic complexity** : services > 500 lignes (chat/router 1100+, files/service 500+, rgpd/data_export_service 600+, planner workers). Découpage ?
- **Naming** : convention `snake_case`/`PascalCase` strict ? Identifiants explicites (vs `data`, `tmp`, `x`) ?
- **Comments** : ratio commentaires/code. Sur-commentaire (verbosity) vs sous-commentaire. CLAUDE.md règle « default no comments » respectée ?
- **Dead code** : TODO Ivan provisoire (recense), code commenté, imports inutilisés (ruff F401), variables inutilisées (F841 ignored ruff V1).
- **DRY violations** : helpers `_encode_cursor`/`_decode_cursor` dupliqués (chat, projects, library, notifications, planner, memory) — extraction shared ?
- **Magic numbers** : `_TITLE_AUTOGENERATE_THRESHOLD=4`, `EXTRACTION_MIN_MESSAGES=6`, `_MIN_IMAGE_DIMENSION=256`. Constantes nommées ✅. Mais hardcodes dispersés dans services ?
- **Type hints** : `Mapped[...]` ORM, `Pydantic v2`, `Literal[...]` enums. Coverage type hints ? `mypy app/` actuellement `ignore_errors=true` V1 (78 erreurs strict).
- **Docstrings** : qualité top sur les modules audité (PromptCache, BudgetTracker, etc.). Cohérent partout ?
- **Imports** : ordre stdlib → third-party → app. Cohérent ?
- **Couplage feature → core** : core/* dépendances. `app/core` → `app/features` interdit (sinon cycle). Vérifier.
- **Lock pyproject.toml** : pas de `uv.lock` committé. Risque reproducibilité ?
- **Dependabot** : `.github/dependabot.yml` 3 updaters weekly. Auto-merge patches/minors. Bonne discipline.
- **CVE pré-existants** : `pypdf 5.9.0` x6 + `pytest 8.4.2` x1 — non bloquants V1 documentés CLAUDE.md.

### D10 — CI/CD & DevEx (note /20)

**Cadre de référence :** DORA metrics 4 keys (deploy frequency, lead time, MTTR, change failure rate), Continuous Delivery (Humble/Farley), GitOps.

**À évaluer :**

- **GitHub Actions workflows** : 7 workflows (ci, release, codeql, dependabot-auto-merge, evals, load, dd-exports-fresh). Permissions least-privilege. Concurrency cancel-in-progress. Versions actions pinned (no @main, no @latest, whitelist 6 orgs).
- **CI pipeline** : 6 jobs (lint, typecheck, security-scan, tests, docker-build, migrations-check). Temps total ? Tests parallèles ?
- **Branch protection** : doc `.github/branch-protection.md` (UI manuelle GitHub limitation). Configurée par Ivan ?
- **Pre-commit** : 7 hooks opt-in (ruff, check-yaml, large-files, merge-conflict, private-key, eof, trailing). Disciplinen.
- **Makefile** : 19 targets clairs avec `## help`. `make ci` enchaîne 4 sub-targets. `make export-dd` regen openapi.json + schema.sql.
- **Docker** : multi-stage (builder Python 3.14 + uv → runtime slim non-root UID 1001 + libpq5 + curl). Healthcheck `/healthz`. `WEB_CONCURRENCY=4` env. Image GHCR.
- **docker-compose.prod.yml** : stub minimal V1 (api + worker), services managés externes (PG/Redis/R2 prévus). Documenté.
- **Release** : `scripts/release.sh` semver bump + tag + push. `scripts/rollback.sh` swap tag + smoke. `scripts/smoke_test.sh` 4 checks read-only. Strict bash `set -euo pipefail`.
- **Évals N3 nightly** : cron 0 3 * * * UTC. Mock PR-blocking (10pp). Real-judge nightly issue-auto (5pp). Fail-safe, soft warning.
- **Load N4 weekly** : cron weekly Sunday 4h UTC. 6 scenarios. Issue auto sur breach.
- **DD freshness check** : `dd-exports-fresh.yml` push main, `git diff --exit-code` openapi.json + schema.sql. Issue auto si stale.
- **Coverage gating** : `fail_under=60` provisoire — V2 75 / V3 80.
- **Deploy frequency** : pas de prod réelle V1. L2 staging à venir.
- **Lead time** : impossible à mesurer V1.
- **Onboarding** : README.md racine 250 lignes — section « Onboarding 5 minutes ». Test pratique ?

### Dt1 — Documentation & DD-readiness (transverse, note /20)

**À évaluer :**

- 7 architecture docs FR avec exec summary EN + Mermaid. Couvre overview / data-model / request-flow / ai-architecture / security-posture / observability / payments-readiness.
- 4 compliance docs (rgpd / ai-act / security-checklist / dpa-template).
- 3 API docs (endpoints / error-codes / versioning) + openapi.json exporté.
- 5 ADRs format Nygard (FastAPI vs Django, SQLAlchemy async, Redis rate limiting, JWT RS256, LlmRouter mock-first). ADRs manquantes : pgvector vs Pinecone, MinIO vs R2, Brevo vs SES, mock-first comme pattern, choix arq vs Celery, etc.
- 3 runbooks (incident-response / deployment-l2 / db-restore). Manquants identifiés.
- glossary.md 50+ termes (NEXYA brand, NYLI, Studio, OHADA, expert, mock-first, etc.).
- README racine 250 lignes onboarding. Tester avec un dev junior.
- CLAUDE.md §15 journal exhaustif (~30 entrées détaillées). Cohérent avec git log ?

### Dt2 — Risques business & opérationnels (transverse, note /20)

**À évaluer :**

- **Single point of failure** : Ivan dev solo. Pas d'équipe ops. Risque bus factor = 1.
- **Vendor lock-in** : Gemini par défaut, Imagen unique provider image. Stratégie diversification ?
- **Cost at scale** : facture LLM worst-case 9 M users. Soutenable ?
- **Time-to-market** : Phase 2 (frontend Flutter) en parallèle. Backend prêt L2 ?
- **Régulation moving target** : AI Act août 2026, futurs ePrivacy, DSA/DMA. Veille active ?
- **Concurrence** : ChatGPT mobile, Claude mobile, Gemini app. Différenciation NEXYA = africa-first + multi-experts + privacy-by-default. Tenu par le code ?
- **Continuité** : pas de DR plan multi-region (Phase 19). Single-region Hetzner Allemagne.

---

## 5. Méthode de lecture des fichiers

Pour chaque fichier audité :

1. **Lecture intégrale** (pas de skim).
2. Note les patterns positifs (à mentionner en « points forts »).
3. Note les anti-patterns (à mentionner en findings).
4. Vérifie la cohérence avec les patterns documentés dans `CLAUDE.md`.
5. Cherche les TODO, FIXME, XXX, HACK, NOTE.
6. Cherche les `# noqa: BLE001` (bare except — fail-safe documenté ou anti-pattern ?).
7. Cherche les `try/except Exception` (fail-safe vs swallow).
8. Cherche les imports lazy (`# noqa: PLC0415`) — circular ?
9. Vérifie les async fonctions n'ont pas d'I/O bloquant (`time.sleep`, `requests`, `open()`).
10. Vérifie les SQL raw n'ont pas de f-string (injection).

Pour les tests :

1. Compte les assertions par test.
2. Vérifie que chaque test a un nom explicite.
3. Vérifie que les fixtures n'ont pas d'effet de bord croisés.
4. Vérifie l'isolation (pas de DB partagée, pas de Redis partagé entre tests).

---

## 6. Classification de sévérité

| Niveau | Définition | Exemple typique |
|---|---|---|
| **S0 — Critique** | Faille exploitable, perte de données, blocage légal RGPD/AI Act, fail-fast manquant en prod | CORS wildcard + credentials prod, IDOR exploitable, password en clair en log, RGPD export fuit cross-user |
| **S1 — Major** | Risque d'incident à 1 M users, dégradation UX significative, dette qui freine le scale | N+1 sur listing chat à 1 M users, pas de read replica, cardinalité Prometheus explose, race condition exploitée par retry client |
| **S2 — Moderate** | Bug latent, comportement inattendu sous charge, drift documentaire significatif | Soft-delete sans cleanup différé MinIO (orphelins), CHECK SQL drifte du Literal Pydantic, runbook absent pour scénario fréquent |
| **S3 — Minor** | Cosmétique, code mort, refactor de confort | TODO Ivan provisoire, helpers dupliqués, commentaire obsolète, dépendance > 12 mois sans bump |

Chaque finding porte : `[Sxx]` titre + fichier:ligne + description + impact + recommandation + effort (S/M/L/XL = 1h/1j/3j/2sem).

---

## 7. Format du rapport final

```markdown
# AUDIT BACKEND NEXYA — 2026-05-01

## Résumé exécutif (1 page max)
- Note globale : XX/20
- Note projetée 12 mois : YY/20
- Top 3 critiques bloquants pour L2 staging
- Top 3 forces remarquables
- Verdict DD investisseur 1 phrase

## Table des matières clicable

## §A — Cartographie & inventaire (5–8 pages)

## §B — Audit dimension par dimension (40–60 pages)
### D1 Architecture (note /20)
### D2 Sécurité (note /20)
### D3 Performance & scalabilité (note /20)
### D4 Tests & qualité (note /20)
### D5 Observabilité & ops (note /20)
### D6 Données & persistance (note /20)
### D7 IA & coût (note /20)
### D8 Conformité (note /20)
### D9 Maintenabilité (note /20)
### D10 CI/CD & DevEx (note /20)
### Dt1 Documentation (note /20)
### Dt2 Risques business (note /20)

## §C — Synthèse transverse (5–10 pages)
- Top 10 Critiques
- Top 20 Important
- Top 30 Nice-to-have
- Matrice priorité × effort
- Roadmap 3 mois / 6 mois / 12 mois
- Risques résiduels par dimension

## §D — Annexes
- Inventaire complet des findings (CSV-like)
- Méthodologie d'évaluation
- Comparatifs externes utilisés
- Limites de l'audit (ce qui n'a PAS été couvert)
```

**Style :**

- Français impeccable (zéro faute, alignement `feedback_french_quality.md`).
- Tableaux Markdown propres.
- Code blocks pour citations `fichier:ligne`.
- Pas d'emojis (CLAUDE.md règle).
- Ton factuel, sans complaisance ni dramatisation.
- Citations directes du code quand pertinent.
- Comparaisons chiffrées (pas « beaucoup », mais « 18 occurrences sur 412 fichiers »).

---

## 8. Garde-fous & honnêteté

- Si tu trouves quelque chose d'extraordinaire, dis-le. NEXYA a beaucoup d'éléments très solides (mock-first 8 SaaS, fail-safe absolu, production safety guard, RGPD complet, observabilité 3 piliers, 1778 tests).
- Si tu trouves quelque chose d'inquiétant, dis-le sans euphémisme.
- Ne mens pas sur ce que tu n'as pas pu vérifier (ex. perf réelle, sécurité runtime). Liste les **limites de l'audit** en annexe.
- Si une dimension dépasse ton expertise (paiements PCI DSS, droit RGPD pointu), dis-le et recommande un expert externe.
- Note attendue 13–17/20 : si tu mets 18+, justifie hors-pair. Si tu mets 12-, justifie hors-pair.

---

## 9. Critères de réussite de l'audit

L'audit est réussi si Ivan peut :

1. Lire le résumé exécutif en 5 minutes et savoir où NEXYA en est.
2. Présenter le rapport à un investisseur sans modification.
3. Démarrer Phase 2 (corrections) en suivant la matrice priorité × effort.
4. Identifier ses 3 prochaines actions immédiates.
5. Comprendre le coût/bénéfice de chaque chantier.
6. Avoir confiance que rien d'important n'a été oublié.

---

## 10. Démarrage

Quand Ivan dit **« Go »** :

1. Affiche le plan d'attaque (chronologie estimée par phase).
2. Démarre la Phase A (cartographie 45 min).
3. Update régulièrement (toutes les 30–60 min) : « phase X — Y % — finding count Z ».
4. Ne demande jamais de validation intermédiaire (autonomie totale).
5. Produis le rapport final dans `docs/audit/AUDIT_BACKEND_2026-05-01.md`.
6. Conclus avec un message d'1 paragraphe : note globale, top 3 critiques, recommandation phase 2.

**Durée maximale estimée : 7 heures.** Si tu en sens le besoin, prends 8h. La qualité prime sur la vitesse.

---

*Fin du prompt. Ce document est l'instruction maîtresse. La TODO d'audit séparée détaille chaque vérification atomique attendue.*
