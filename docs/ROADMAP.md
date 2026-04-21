# 🚀 ROADMAP NEXYA — Suivi exécutif

> **Source de vérité unique** pour le développement, le lancement, la croissance et la gouvernance de NEXYA.
> **Version :** 2.0 consolidée (16 phases techniques + 4 phases business + apprentissage transversal)
> **Dernière mise à jour :** 2026-04-21
> **Auteur :** Loth Ivan Ngassa Yimga

---

## 📖 Légende

- `[ ]` à faire
- `[x]` fait
- `[~]` en cours
- `[!]` bloqué (préciser pourquoi en commentaire)

---

## ⚡ Trois règles non négociables

1. **Phase 0 démarre AUJOURD'HUI.** Société + marque + domaines = 6 mois de délai administratif. Si tu attends, ils bloqueront tout le reste.
2. **Modération + garde-fous AVANT première soumission Apple.** Sans ça → rejet certain.
3. **Pentest AVANT 1000 users payants.** Une faille découverte par un user qui fuite = fin de l'aventure.

---

## 🧭 Comment utiliser ce fichier

1. **Ouvrir au début de chaque session de travail.**
2. **Une seule phase à la fois** (sauf Phase 0 + Phase 17 qui tournent en parallèle dès le début).
3. **Le livrable est le contrat** : pas de phase suivante avant livrable validé (vidéo démo + tests verts + doc à jour).
4. **Apprentissage en prefetch** : pendant la phase N, lire la ressource de la N+1.
5. **Mise à jour CLAUDE.md** à la fin de chaque phase (journal + ✅).
6. **Revue hebdo** : 30 min vendredi, faire le point progression + ajuster.

---

# SECTION 0 — État consolidé du projet

## Backend (`nexya_backend/`) — ~43 % couvert

### ✅ Fait
- [x] FastAPI + lifespan + middlewares (CORS, GZip, Trace, Errors, RateLimit)
- [x] Auth de base (register, login, refresh rotation, logout, /me, blacklist Redis, RS256)
- [x] User model + soft-delete + plan/quotas
- [x] PostgreSQL 16 + pgvector + Alembic + psycopg async (port 5433 sur Windows)
- [x] Redis 7 (cache + blacklist + rate-limit + arq)
- [x] MinIO container
- [x] arq worker + cron `cleanup_refresh_tokens` 03:17 UTC
- [x] Dockerfile multi-stage non-root UID 1001
- [x] structlog + TraceIdMiddleware + scrubber secrets
- [x] Tests hardening (9/9 ✅)
- [x] `/healthz` + `/ready`
- [x] `/chat/stream` SSE refactoré via Couche IA (budget → modération → fallback chain → heartbeat 15 s → annulation Redis + disconnect)
- [x] `/chat/stop` (annulation via clé Redis `chat:cancel:{session_id}`)
- [x] `/image/generate` refactoré via Couche IA (budget → modération → router → Imagen 3)
- [x] `app/seed.py` comptes démo (free + pro)
- [x] **Couche IA (Tier 1, 7 briques)** — ABC `LlmProvider` + types neutres + erreurs typées, GeminiProvider réel + 3 stubs (OpenAI/Anthropic/Qwen), LlmRouter (resolve + chain + image) + 11 ExpertConfig, ModerationService OpenAI fail-open, BudgetTracker Redis (chat/img/IP/modèle), Retry exponentiel + jitter, CircuitBreaker `(provider,model)` (CLOSED/OPEN/HALF_OPEN), StreamHandler SSE, Observabilité (`StreamMetrics` + `estimate_cost_usd` + log unique `ai.chat.completed`)
- [x] **Chat persisté — Lot 1 (fondation data)** — modèles ORM `Conversation` / `Message` / `AbuseReport`, migration Alembic 002 (5 indexes dont 1 partiel, 6 CHECK), 11 schémas Pydantic v2 avec compat descendante `history=[...]`, enregistrement autogenerate
- [x] **Chat persisté — Lot 2 (service)** — `ConversationService` : CRUD (`create`, `list_for_user` keyset `COALESCE(last_message_at, created_at) DESC`, `get_by_id`, `update` partiel, `soft_delete`, `list_messages` keyset ASC), helper `_get_owned_conversation()` (IDOR → 404 jamais 403), curseur opaque base64 `{iso}|{uuid}` avec `ValidationException` 422 si malformé, `_bump_counters()` atomique sans commit pour Lot 4, `ValidationException` ajoutée au catalogue + 7 tests unit verts sans DB
- [x] **Chat persisté — Lot 3 (router CRUD)** — 6 endpoints `/chat/conversations` (POST, GET paginé + filtres `is_archived`/`is_favorite`, GET/PATCH/DELETE `{id}`, GET `{id}/messages`) + `ConversationsPage` Pydantic + 16 tests router via `TestClient` + `dependency_overrides` (happy-path × 6, IDOR cross-user 404 × 4, curseur forgé 422, UUID malformé 422, titre vide rejeté, `limit > 50` rejeté)
- [x] **Chat persisté — Lot 4 (`/chat/stream` persisté)** — placeholder assistant inséré avant la chaîne provider (`start_stream_turn` : user + assistant dans la même transaction, `_bump_counters(delta=2)`) → finalisation atomique via `finalize_assistant_stream()` dans une session `AsyncSessionLocal` neuve protégée par `asyncio.shield()` (survit à la déconnexion client) → status `completed`/`failed`/`cancelled` dérivé de l'événement SSE `done` ; nouveau module `app/ai/runtime.py` (`get_ai_router()` / `get_stream_handler()`) pour casser la circulaire `main.py ↔ features/chat/router.py` ; extraction `/chat/stream` + `/chat/stop` de `main.py` vers le router chat ; dispatch 3-modes (legacy stateless / nouvelle conv / conv existante) ; 18 tests verts (`test_chat_stream_persisted.py`)
- [x] **Chat persisté — Lot 5 (auto-titre + signalements)** — worker arq `generate_conversation_title` (Gemini Flash sur les 6 derniers messages `completed`, `_sanitize_title()`, persistance idempotente via sentinelle `title_generated_at`, coût worst-case ≈ $475/mois à 950 k users) + hook depuis `_finalize_in_fresh_session()` (seuil `>= 4` messages `completed`) ; `POST /chat/reports` → owner check via JOIN (rempart IDOR unique), `IntegrityError → 409 DUPLICATE_REPORT` (TOCTOU-safe, pas de pré-SELECT), rate limit user-scoped `rate:user:abuse_report:{uid}` 10/h via Redis sliding-window + `RateLimitAbuseException` 429 distinct de `RATE_LIMIT_IP` (UX différente côté Flutter) ; fix collatéral `nexya_exception_handler` propage `exc.data` dans `NexyaResponse.data` (auparavant perdu — bloquait la lecture de `retry_after`) ; 13 tests Lot 5 verts ; suite globale 63/63 verts
- [x] **Chat persisté — F2.0 (corbeille + filtre expert)** — `GET /chat/conversations/trash` (keyset `deleted_at DESC`), `POST /{id}/restore`, `DELETE /{id}/permanent` (DELETE physique + cascade SQL, exige état corbeille — flow two-step) ; helper `_get_owned_conversation_in_trash()` symétrique ; filtre query `expert_id` sur la liste active ; champ `deleted_at` exposé dans `ConversationResponse` + `ConversationListItem` ; 11 tests (suite chat 16 → 27 verts)
- [x] **Validation end-to-end manuelle (2026-04-21)** — tous les flux Lots 1-5 + F2.0 jouées via `curl` contre la vraie stack PostgreSQL+Redis. Bug réel découvert + corrigé : `ReportService.create_report` plantait `MissingGreenlet` sur le doublon (lazy-load `user.id` post-rollback → `pool_pre_ping` sync). Fix : capture `str(user.id)` / `str(message.id)` avant le `try/commit`. Suite pytest 63/63 toujours verte.

### ❌ Reste
- [ ] OpenTelemetry + OTLP, Sentry, dashboards Grafana
- [ ] Reset password email, OAuth Google/Apple, captcha hCaptcha, modération
- [ ] Quotas par device fingerprint, anti-abus avancé
- [ ] Index pgvector HNSW peuplés, recherche full-text français
- [ ] Service S3 wrapper, signed URLs, antivirus optionnel
- [ ] Jobs IA longs, jobs notifications, jobs purge RGPD
- [ ] docker-compose prod, Caddy/Traefik, K8s optionnel
- [ ] Tests par feature, évals IA en CI
- [ ] Watermarking C2PA images générées + attribution sources RAG (la Couche IA + modération sont livrées en 2026-04-21)
- [ ] Seed corpus experts (RAG)
- [ ] 12 features métier (Chat persisté, History, Projects, Planner, Voice, Vision, Files, Library, Memory, Notifications, Subscriptions, Settings)
- [ ] Versioning `/v1/`, deprecation policy, forced-update endpoint
- [ ] WAF/DDoS Cloudflare

## Frontend (`nexya_front_end/`) — ~18 % couvert

### ✅ Fait
- [x] Riverpod 3.1 + go_router + dio + flutter_secure_storage 10
- [x] Design tokens + thème dark/light
- [x] Splash animé + onboarding 3 écrans
- [x] Widgets `Nx*` (Button, ChatBubble, InputBar, Drawer, ModelPill, Avatar…)
- [x] ChatScreen UI + InputBar + streaming SSE consommé
- [x] `secure_storage.dart` (vrai wrapper FlutterSecureStorage)
- [x] `api_client.dart` (Dio + refresh chain)
- [x] `auth_interceptor.dart` (QueuedInterceptor + mutex)
- [x] `retry_interceptor.dart` (backoff + jitter)
- [x] `chat_remote_datasource.dart` injecte JWT
- [x] i18n ARB FR + intl 0.20
- [x] Suppression dead code LLM providers (openai, gemini, qwen, whisper, local_llm, llm_factory…)

### ❌ Reste
- [ ] Accessibilité WCAG AA, dynamic type, mode contraste élevé
- [ ] Page intro pré-login (slogan + CTA)
- [ ] NxToast, NxDialog, NxBottomSheet standardisés
- [ ] Login/Register/Forgot/Reset, profil, suppression compte
- [ ] `auth_repository`, `auth_controller`, redirect guards
- [ ] History persistée, projets, planner, voice, vision, files, library, settings
- [ ] EN, anglais Nigeria, swahili, wolof, lingala, bambara, RTL
- [ ] Stripe/CinetPay/RevenueCat, paywall, restore purchases
- [ ] Push FCM + APNs, deep links, App Tracking Transparency, Privacy Manifest iOS
- [ ] Mode tablette/iPad, mode hors-ligne, mode économie de données
- [ ] Tests widget + intégration, golden tests, performance budget
- [ ] Obfuscation Flutter, ProGuard/R8, certificate pinning client

## IA / Modèles — ~28 %

### ✅ Fait
- [x] ABC `LlmProvider` + types neutres (ChatMessage, ChatChunk, ChatCompletionRequest, ImageGenerationRequest, ChatUsage)
- [x] Hiérarchie d'erreurs typées (`ProviderError` / `Unavailable` / `RateLimit` / `Auth` / `ContentFiltered` / `InvalidRequest`, flag `retryable`)
- [x] GeminiProvider réel (chat streaming + Imagen 3)
- [x] OpenAI / Anthropic / Qwen providers (stubs ABC, prêts pour câblage SDK)
- [x] LlmRouter — `resolve(expert_id)` + `build_chain(expert_id)` + `resolve_image(expert_id)` + factory `build_default_router`
- [x] ContextBuilder — 11 `ExpertConfig` (general + 10 experts) avec system prompt, tier modèle Flash/Pro, fallback chain, disclaimers métiers
- [x] ModerationService OpenAI `omni-moderation-latest` (fail-open 3 s, désactivable)
- [x] BudgetTracker Redis — quotas chat/img user/jour, IP burst/min, cap modèle global, INCR + DECR rollback atomique
- [x] Retry exponentiel + jitter (retry uniquement avant 1ᵉʳ chunk, honore `retry_after_seconds`)
- [x] CircuitBreaker par `(provider, model)` (CLOSED → OPEN → HALF_OPEN, in-memory thread-safe)
- [x] StreamHandler SSE (heartbeat 15 s, annulation duale `is_disconnected()` + Redis, traversée chaîne fallback)
- [x] Observabilité tokens — StreamMetrics + table prix USD/1M tokens + `estimate_cost_usd` + log unique `ai.chat.completed`

### ❌ Reste
- [ ] CostTracker DB (persistance utilisateur — table `ai_calls` + `usage_daily`, à brancher Phase 4)
- [ ] SessionStore (Redis TTL 24 h + flush PostgreSQL)
- [ ] QueryEngine consolidé (logique actuellement répartie `streaming.py` + `main.py`)
- [ ] Garde-fous métiers actifs côté modération (refus prescription médicale, conseil juridique nominatif…)
- [ ] Cache Redis sur `(model, hash(prompt))` TTL 24 h pour économies
- [ ] Câblage SDK réel pour OpenAI / Anthropic / Qwen (les stubs lèvent `ProviderUnavailableError`)
- [ ] RAG + embeddings + retrieve + attribution sources
- [ ] Fine-tuning LoRA Gemma + dataset versioning DVC
- [ ] Modèles locaux (Ollama VPS GPU) + mobile-embarqué optionnel
- [ ] Modes Experts spécialisés (Langues, Cuisine, Studio, Ingénierie, Productivité, Informatique, Sciences/Maths) — system prompts en place, RAG/corpus à indexer
- [ ] Watermarking C2PA images générées
- [ ] Évaluations CI + drift detection + red-teaming + model registry

## Infra / DevOps — ~10 %

### ✅ Fait
- [x] docker-compose dev
- [x] Dockerfile prod multi-stage
- [x] Alembic migrations

### ❌ Reste
- [ ] CI/CD GitHub Actions
- [ ] Staging Hetzner CX32
- [ ] Prod Hetzner CX42/CCX13
- [ ] WAF Cloudflare + anti-DDoS
- [ ] Monitoring + alerting
- [ ] Sauvegardes testées + DR plan
- [ ] Status page publique
- [ ] Runbooks + postmortems

## Légal / Conformité / Business — ~2 %

### ✅ Fait
- [x] Repo Git versionné, structure projet

### ❌ Reste
- [ ] Société (RCCM, NIU)
- [ ] Marque OAPI déposée
- [ ] Domaines défensifs (.com, .ai, .cm, .app)
- [ ] Comptes bancaires pros
- [ ] Comptable identifié
- [ ] Juriste identifié
- [ ] CGU/CGV/Politique confidentialité
- [ ] DPA fournisseurs (OpenAI, Google, Anthropic, RevenueCat, Sentry, Cloudflare, Hetzner)
- [ ] Conformité loi 010/2010 Cameroun + RGPD + EU AI Act 2026
- [ ] Assurance RC pro + cyber
- [ ] Comptabilité + facturation
- [ ] Brand kit + landing nexya.ai
- [ ] Support helpdesk + communauté

> **Vue d'ensemble : ~17 % du produit final.**

---

# PHASE 0 — Fondations légales & administratives (M, démarre EN PARALLÈLE de tout)

**Statut global :** [ ]

**Objectif.** Avoir une entité légale, une marque protégée, des domaines réservés, des comptes pros, un juriste et un comptable. Sans ça : pas de RevenueCat, pas de Stripe, pas de DPA conformes, pas de facturation légale, risque de squat de marque.

### Pré-requis à apprendre
- [ ] Formes juridiques camerounaises (SARL, SARLU, SA)
- [ ] OHADA — bases du droit des affaires Afrique francophone
- [ ] Loi camerounaise n° 010/2010 sur protection des données personnelles
- [ ] RGPD bases (utilisateurs UE via diaspora)
- [ ] EU AI Act — obligations transparence systèmes IA usage général

### Tâches
- [ ] Réserver `nexya.ai` (prioritaire)
- [ ] Réserver `nexya.com`
- [ ] Réserver `nexya.cm`
- [ ] Réserver `nexya.app`
- [ ] Réserver `getnexya.com`
- [ ] Créer SARLU (capital ~100k FCFA) au CFCE Yaoundé/Douala
- [ ] Obtenir RCCM
- [ ] Obtenir NIU
- [ ] Obtenir attestation immatriculation
- [ ] Ouvrir compte bancaire pro (Afriland / SocGen Cameroun / Wise)
- [ ] Déposer marque "NEXYA" + logotype à l'OAPI (~300k FCFA, 6-12 mois)
- [ ] Identifier juriste freelance (Malt/LinkedIn) pour CGU/CGV/Privacy (200-500 €)
- [ ] Identifier comptable (cabinet local ou Pennylane Africa)
- [ ] Souscrire assurance RC pro + cyber-risque (Hiscox, Allianz Afrique)
- [ ] Préparer DPA templates : OpenAI, Google Cloud, Anthropic, RevenueCat, Sentry, Cloudflare, Hetzner
- [ ] Documenter dans Notion privé (statuts, RCCM, contrats, tokens, mots de passe via Bitwarden)

### Livrable
- [ ] Société immatriculée
- [ ] Compte bancaire actif
- [ ] Marque déposée en cours
- [ ] 5 domaines réservés
- [ ] Juriste + comptable identifiés
- [ ] DPA prêts à signer

### Risques
- Lenteur administrative camerounaise — démarrer immédiatement.
- Squat de marque/domaine si tu attends.

---

# PHASE 1 — Backend Auth durci (S-M)

**Statut global :** [~] (auth de base faite, reste reset + OAuth + captcha + anti-abus)

**Objectif.** Backend Auth livrable production : reset password email, OAuth Google/Apple, captcha anti-bot, signalement compte compromis.

### Pré-requis à apprendre
- [x] JWT RS256 + clés asymétriques + rotation
- [ ] OAuth 2.0 / OpenID Connect (flow Authorization Code + PKCE pour mobile)
- [ ] SendGrid ou Brevo : emails transactionnels + templates Jinja2
- [ ] hCaptcha ou Cloudflare Turnstile : intégration server-side

### Tâches
- [ ] `POST /auth/forgot-password` → token signé 15 min → email via SendGrid
- [ ] `POST /auth/reset-password` → consomme token + hash bcrypt + invalide tous refresh tokens
- [ ] `GET /auth/google/start` + `/auth/google/callback`
- [ ] `GET /auth/apple/start` + `/auth/apple/callback` (Sign In with Apple)
- [ ] `POST /auth/register` exige token captcha hCaptcha validé server-side
- [ ] Détection comportement suspect : >100 messages/min → block 1h
- [ ] Limite >5 inscriptions par IP/jour → block
- [ ] Logs sécurité : table `auth_events` (tentatives, IP, user-agent)
- [ ] Tests hardening complémentaires : reset, OAuth, race-condition refresh, captcha
- [ ] Documentation Swagger + CLAUDE.md §journal

### Livrable
- [ ] Toutes routes `/auth/*` vertes Swagger
- [ ] `pytest tests/auth/` 100 %
- [ ] Démo OAuth Google fonctionnelle
- [ ] Captcha bloque les bots

### Risques
- OAuth redirect URIs différentes par env → variable `OAUTH_REDIRECT_URI` par environnement.

---

# PHASE 2 — Couche IA backend + Safety/Modération (M)

**Statut global :** [~] (Tier 1 livré 2026-04-21 — reste : SDK réels OpenAI/Anthropic/Qwen, cache Redis prompt, attribution sources RAG, garde-fous métiers actifs)

**Objectif.** Centraliser tous les appels modèles dans une couche unique : interface stable, fallback, budget, observabilité tokens, modération entrée/sortie, garde-fous médicaux/légaux, attribution sources.

### Pré-requis à apprendre
- [x] Pattern Strategy + Adapter Python
- [x] SDKs : OpenAI, `google-generativeai`, `anthropic`, Mistral, OpenRouter (Gemini intégré, autres en stub)
- [ ] `tiktoken` pour estimer le coût avant l'appel (estimation actuelle via tokens retournés par le provider)
- [x] OpenAI Moderation API (gratuite) + alternatives (Perspective API)

### Tâches
- [x] `app/ai/providers/base.py` : ABC `LlmProvider` + types neutres + erreurs typées
- [~] `OpenAIProvider` (stub conforme à l'ABC, branchement SDK à finaliser)
- [x] `GeminiProvider` (chat streaming + Imagen 3)
- [~] `AnthropicProvider` (stub conforme à l'ABC, branchement SDK à finaliser)
- [~] `QwenProvider` (stub conforme à l'ABC, branchement SDK à finaliser)
- [ ] `MistralProvider` (non prioritaire — couvert par OpenRouter)
- [ ] `OpenRouterProvider`
- [x] `app/ai/router.py` (sélection selon expert + fallback auto, factory `build_default_router`)
- [~] `app/ai/budget.py` : `BudgetTracker` Redis live (chat/img user/jour, IP burst, cap modèle) — table DB `ai_calls` à ajouter Phase 4
- [ ] `app/ai/cache.py` : cache Redis sur `(model, hash(prompt))` TTL 24h
- [x] `app/ai/moderation.py` : check entrée + sortie (fail-open 3 s)
- [~] Garde-fous métiers : disclaimers médecin/avocat injectés en prefix par StreamHandler — refus prescription actif côté modération à brancher
- [ ] Attribution sources dans réponses RAG (`sources: [...]`)
- [x] Refactor `/chat/stream` pour passer par le router (+ `/chat/stop` + `/image/generate`)
- [x] **Bonus** Retry exponentiel + jitter + CircuitBreaker `(provider, model)` + StreamMetrics + estimateur coût USD + log unique `ai.chat.completed`

### Livrable
- [x] `/chat/stream` route vers n'importe quel modèle (résolu par `LlmRouter` — chaîne primaire + fallbacks)
- [x] Log chaque appel + coût (`ai.chat.completed` avec `provider/model/prompt_tokens/completion_tokens/cost_usd/duration_ms/outcome`)
- [~] Fallback prouvé en test (chaîne traversée + circuit breaker — test d'intégration end-to-end à ajouter avec kill API key)
- [ ] Modération bloque 10 prompts toxiques test (à valider en intégration avec clé OpenAI réelle)
- [~] Garde-fous médical actifs (disclaimer prefix injecté en chunk 1 — refus prescription côté modération à brancher)

### Risques
- Coûts qui explosent — budget global 5 $ + alertes 80 %.
- Latence ajoutée par modération (~150 ms) — non négociable pour Apple review.

---

# PHASE 3 — Frontend Auth de bout en bout (M)

**Statut global :** [ ] (infrastructure prête en Phase A, reste UI + intégration)

**Objectif.** Parcours complet : intro → onboarding → inscription/connexion (email + OAuth) → session persistante → reset → suppression compte. Avec deep links et conformité iOS.

### Pré-requis à apprendre
- [ ] Riverpod 3.1 : `Notifier`, `AsyncNotifier`, `ProviderObserver`
- [ ] `go_router` : `redirect` global pour gating auth
- [ ] Form validation Flutter (`Form`, `TextFormField`, validators)
- [ ] Universal Links iOS + App Links Android
- [ ] App Tracking Transparency iOS 14.5+
- [ ] Privacy Manifest iOS (`PrivacyInfo.xcprivacy`)

### Tâches
- [ ] Page intro pré-login (slogan + CTA "Commencer")
- [ ] Refactor onboarding 3 écrans → "S'inscrire" / "J'ai un compte"
- [ ] `auth/data/auth_remote_datasource.dart` (register, login, logout, refresh, me, forgot, reset, googleSignIn, appleSignIn)
- [ ] `auth/data/auth_repository.dart` retournant `Result<User, AuthFailure>` (sealed class)
- [ ] `auth/logic/auth_controller.dart` (`AsyncNotifier<User?>`)
- [ ] `LoginScreen`
- [ ] `RegisterScreen` (avec captcha widget)
- [ ] `ForgotPasswordScreen`
- [ ] `ResetPasswordScreen`
- [ ] `OAuthCallbackScreen`
- [ ] `app_router.dart` : `redirect` global vers `/login` si non auth
- [ ] Intégration `google_sign_in` package
- [ ] Intégration `sign_in_with_apple` package
- [ ] Configurer Universal Links (`apple-app-site-association`) côté backend
- [ ] Configurer App Links (`assetlinks.json`) côté backend
- [ ] Ajouter `NSUserTrackingUsageDescription` Info.plist
- [ ] Appel ATT au bon moment (pas au boot)
- [ ] Créer `PrivacyInfo.xcprivacy` (API "required reason")
- [ ] Page Settings → "Supprimer mon compte" → `DELETE /users/me` + double confirmation

### Livrable
- [ ] Démo : install → onboarding → inscription captcha → email vérif → entrée app → ferme/relance → toujours connecté → reset password → reconnecté
- [ ] Deep link `https://nexya.ai/conv/abc` ouvre la conversation dans l'app

### Risques
- Keychain iOS : tester sur device réel, pas simulateur.
- Boucle refresh : flag `authRetried` (déjà en place).

---

# PHASE 4 — Chat MVP persisté + signalement abus (M)

**Statut global :** [~] **Backend complet ✅** (Lots 1-2-3-4-5 + F2.0 livrés et validés end-to-end le 2026-04-21 — fix `MissingGreenlet` post-rollback dans `ReportService.create_report` corrigé en cours de validation manuelle). Reste : volet Frontend (`ChatRepository`, `ChatListScreen`, `ChatController`, bouton signalement).

**Objectif.** L'utilisateur retrouve ses conversations passées, en démarre de nouvelles, le streaming temps réel fonctionne, l'historique est sauvé, chaque réponse est signalable.

### Pré-requis à apprendre
- [ ] Modélisation conversationnelle
- [ ] Optimistic UI
- [ ] Pattern Repository côté Flutter
- [ ] Cache local Hive ou Drift

### Tâches
- [x] **Backend — Lot 1** Modèles `Conversation`, `Message`, `AbuseReport` + migration Alembic 002 (5 indexes dont 1 partiel, 6 CHECK constraints, FK cascade, soft-delete `deleted_at`, dénormalisation `last_message_at` + `message_count`) + 11 schémas Pydantic v2 (`ChatStreamRequest` avec compat descendante `history=[...]`) + enregistrement `migrations/env.py`
- [x] **Backend — Lot 2** Service `ConversationService` : CRUD complet (`create`, `list_for_user` paginé keyset, `get_by_id`, `update`, `soft_delete`, `list_messages`) + helper `_get_owned_conversation()` (isolation IDOR → 404 jamais 403) + curseur opaque base64 (`{iso}|{uuid}`) avec `ValidationException` si malformé + `_bump_counters()` atomique sans commit (appelé par Lot 4 dans la transaction du stream) + 7 tests unit verts (cursor round-trip, 4 cas malformés, 2 cas isolation)
- [x] **Backend — Lot 3** Router `/chat/conversations` : 6 endpoints (`POST`, `GET` paginé + filtres `is_archived`/`is_favorite` + `cursor`/`limit`, `GET {id}`, `PATCH {id}` partiel, `DELETE {id}` soft → 204, `GET {id}/messages` paginé) + enregistrement `main.py` + schéma Pydantic `ConversationsPage` + renommage `ConversationsPage` ORM → `ConversationsPageOrm` (cohérence `MessagesPageOrm`) + 16 tests router verts via `TestClient` avec `dependency_overrides` + monkeypatch `AsyncMock` du service (happy-path × 6, isolation cross-user 404 × 4, curseur forgé 422, UUID malformé 422, titre vide rejeté, `limit>50` rejeté par Pydantic)
- [x] **Backend — Lot 4** Refactor `/chat/stream` persisté livré (2026-04-21) : placeholder `Message(role='assistant', status='streaming')` inséré avant la chaîne provider par `ConversationService.start_stream_turn()` (user + assistant dans la même transaction, `_bump_counters(delta=2)`) → finalisation atomique via `ConversationService.finalize_assistant_stream()` appelée depuis `_finalize_in_fresh_session()` dans un `AsyncSessionLocal` neuf + `asyncio.shield()` pour survivre à la déconnexion client → status `completed` / `failed` / `cancelled` dérivé de l'événement SSE `done` (mapping `_DONE_REASON_TO_STATUS`) + `error_code` persisté sur échec non-retryable ; `StreamContext` étendu d'un champ `metrics: StreamMetrics | None` pour que le router lise provider/model/usage/cost_usd sans modifier la sémantique de yield du `StreamHandler` ; extraction de `/chat/stream` + `/chat/stop` de `main.py` vers `features/chat/router.py` + nouveau module `app/ai/runtime.py` (`get_ai_router()` / `get_stream_handler()`) pour casser la circulaire ; dispatch 3-modes (legacy stateless `history=[...]` / nouvelle conv persistée / conv existante), header `X-Conversation-Id` sur les modes persistés ; 18 tests verts (`tests/test_chat_stream_persisted.py` : parsing SSE, mapping statuts, `ensure/start/finalize`, `/chat/stop`, happy-path, error path, modération bloquée, message vide 422)
- [x] **Backend — F2.0 Corbeille + filtre expert** Livré (2026-04-21) : `GET /chat/conversations/trash` (keyset `deleted_at DESC, id DESC` + filtre optionnel `expert_id`), `POST /chat/conversations/{id}/restore` (efface `deleted_at`, ne bump pas `last_message_at` pour préserver le classement actif), `DELETE /chat/conversations/{id}/permanent` (DELETE physique + `ON DELETE CASCADE` sur messages + abuse_reports, exige état corbeille — two-step flow garanti) ; helper `_get_owned_conversation_in_trash()` symétrique de `_get_owned_conversation()` (sépare strictement monde actif vs monde corbeille) ; query param `expert_id` ajouté à `list_for_user()` ; champ `deleted_at: datetime | None` exposé dans `ConversationResponse` + `ConversationListItem` (peuplé sur trash/restore, null sur les endpoints actifs grâce au filtre SQL `deleted_at IS NULL`) ; 11 nouveaux tests (suite 16 → 27 verts) couvrant happy-path corbeille, forward `expert_id`, garde anti-régression précédence route `/trash` vs `/{id}`, restore happy-path + 404 IDOR-safe + 422 UUID malformé, permanent_delete 204 + 404 IDOR-safe, exposition `deleted_at` dans le contrat
- [x] **Backend — Validation end-to-end manuelle** (2026-04-21) : tous les flux Lots 1-5 + F2.0 validés via `curl` end-to-end contre une vraie stack PostgreSQL+Redis (CRUD, corbeille, restore, permanent, `/chat/stream` persisté, `/chat/stop` annulation SSE → `cancelled` en DB, `/chat/reports` happy-path 201, doublon → 409 `DUPLICATE_REPORT`). Bug réel découvert et corrigé pendant la validation : `ReportService.create_report` plantait en `MissingGreenlet` sur le doublon — après `db.rollback()`, le `str(user.id)` du log déclenchait un lazy-load → `pool_pre_ping` → setattr sync `autocommit=True` sur la connexion psycopg → exception. Fix : capture des UUID en `str` AVANT le `try/commit` pour ne pas accéder à des attributs ORM expirés post-rollback. Suite pytest reste 63/63 verte.
- [x] **Backend — Lot 5** Livré (2026-04-21) : worker arq `generate_conversation_title` (`workers/chat_tasks.py`) — appel Gemini Flash via `LlmRouter.resolve("general")` sur les 6 derniers messages `completed`, prompt court ≤ 60 chars, `_sanitize_title()` (strip + dégarnir guillemets typographiques + troncature), persistance atomique `UPDATE conversations SET title, title_generated_at, updated_at WHERE id = ? AND title_generated_at IS NULL` (sentinelle one-shot anti-doublon), double-check défensif en entrée du worker + garde `len(rows) < 2`, coût worst-case ≈ $475/mois à 950 k users ; helper module-level `enqueue_title_generation(conversation_id)` (pool arq lazy, imports arq internes au `_get_arq_pool()` pour ne pas forcer la dépendance à l'import des tests, échec silencieux si Redis down) ; hook `_finalize_in_fresh_session()` → enqueue si `status='completed' AND message_count >= 4 AND title_generated_at IS NULL AND title IS NULL` (seuil `>= 4` et non `== 4` → résilience aux pannes d'enqueue, la sentinelle protège des doublons) ; registration dans `workers/worker.py` (`functions=[cleanup_refresh_tokens, generate_conversation_title]`) ; `POST /chat/reports` (201 + `NexyaResponse[AbuseReportResponse]`) délègue à `ReportService.create_report()` : owner check via `_get_owned_message()` (JOIN `Message + Conversation` — rempart IDOR unique), `INSERT AbuseReport` avec `conversation_id` dénormalisé depuis le message, `IntegrityError` (UNIQUE `user_id, message_id`) → `db.rollback()` + `DuplicateReportException` 409 `DUPLICATE_REPORT` (pas de pré-SELECT anti-TOCTOU) ; rate limit user-scoped `rate_limit_abuse_reports()` (sliding window Redis `rate:user:abuse_report:{uid}` 10/h, `RateLimitAbuseException` 429 `RATE_LIMIT_ABUSE` avec `retry_after` dans `data`, code distinct de `RATE_LIMIT_IP` pour que le Flutter puisse distinguer anti-brute-force vs anti-spam UI) ; fix collatéral `handlers.py` → `nexya_exception_handler` propage désormais `exc.data` dans `NexyaResponse.data` (auparavant perdu, bloquait la lecture de `retry_after` côté client) ; 13 tests Lot 5 verts (9 `test_abuse_reports.py` : service happy-path + IDOR 404 + IntegrityError → 409 + router 201/422×2/404/409/429, 4 `test_chat_stream_persisted.py` étendus : enqueue si threshold atteint, skip si `< 4` / sentinelle déjà posée / `status='failed'`), suite complète 63/63 verts
- [ ] **Frontend** `ChatRepository` (datasource + cache local Hive)
- [ ] **Frontend** `ChatListScreen` tri par `updated_at`
- [ ] **Frontend** `ChatController` (`AsyncNotifier`) append SSE en place + sauve local
- [ ] **Frontend** Bouton "Nouvelle conversation" + suppression swipe
- [ ] **Frontend** Bouton "Signaler" sur chaque message IA → modal reason → POST

### Livrable
- [ ] Vidéo démo : login → liste conversations → ouvre ancienne → message streaming → ferme app → rouvre → message présent
- [ ] Bouton signalement opérationnel

### Risques
- Désync local/serveur si l'user change de device → source de vérité = serveur.

---

# PHASE 5 — History, Projects, Library (M)

**Statut global :** [ ]

**Objectif.** Trois fonctions transversales : retrouver activité, organiser conversations en projets, sauvegarder contenu généré.

### Pré-requis à apprendre
- [ ] Recherche full-text PostgreSQL : `tsvector`, `GIN`, `pg_trgm`, dictionnaire `french`
- [ ] Pagination cursor-based

### Tâches
- [ ] **Backend** Table `projects` (user_id, name, color, system_prompt)
- [ ] **Backend** FK `conversations.project_id` nullable
- [ ] **Backend** Table `library_items` (user_id, type, url MinIO, mime, size, conversation_id, tags)
- [ ] **Backend** `GET /history?cursor=&limit=&q=` avec FTS français + `pg_trgm`
- [ ] **Backend** CRUD `/projects`
- [ ] **Backend** CRUD `/library`
- [ ] **Frontend** `HistoryScreen` (recherche + scroll infini)
- [ ] **Frontend** `ProjectsScreen` + sélecteur dans drawer
- [ ] **Frontend** `LibraryScreen` (onglets Images/Fichiers/Notes)

### Livrable
- [ ] Démo : créer projet "École", assigner 3 conversations, chercher "intégrale" → résultats, sauver image dans library, la retrouver

### Risques
- Index FTS français : `to_tsvector('french', content)` + `GIN`.

---

# PHASE 6 — Mémoire pgvector + RAG + attribution (L)

**Statut global :** [ ]

**Objectif.** L'IA "se souvient" de l'utilisateur à travers conversations, et répond depuis documents uploadés. Chaque réponse RAG cite ses sources.

### Pré-requis à apprendre
- [ ] Embeddings : OpenAI `text-embedding-3-small` (1536d) ou `bge-m3`
- [ ] pgvector : HNSW vs IVFFlat, distance cosinus
- [ ] Chunking : `RecursiveCharacterTextSplitter` (~500 tokens, overlap 50)
- [ ] Pattern RAG : retrieve → re-rank → inject → generate
- [ ] Dataset versioning DVC ou HF Hub privé

### Tâches
- [ ] **Backend** Table `memories` + index HNSW
- [ ] **Backend** Job arq post-conversation : extrait 0-3 faits durables
- [ ] **Backend** Inject top-5 memories dans system prompt
- [ ] **Backend** Service `documents` : upload PDF/TXT/MD MinIO → extraction → chunking → embedding → insert
- [ ] **Backend** `POST /chat/stream-rag` retourne `sources: [...]`
- [ ] **Backend** Versionner corpus avec DVC (push S3/MinIO)
- [ ] **Frontend** Page "Ma mémoire" (liste faits + suppression individuelle RGPD)
- [ ] **Frontend** Bouton joindre fichier dans InputBar
- [ ] **Frontend** Affichage `sources` sous réponses RAG (cards cliquables)

### Livrable
- [ ] L'utilisateur dit "Je suis dev Flutter" → 3 jours plus tard "écris-moi du code" → IA propose Dart sans re-précision
- [ ] Upload PDF → question dessus → réponse avec sources cliquables

### Risques
- Coût embeddings → batch + cache Redis hash chunk.
- RGPD : `DELETE /memories/{id}` obligatoire.

---

# PHASE 7 — Voice + Vision + Files + Watermarking (M)

**Statut global :** [ ]

**Objectif.** Trois modalités au-delà du texte ; images générées watermarkées C2PA pour conformité AI Act.

### Pré-requis à apprendre
- [ ] STT : Whisper API ou `faster-whisper` self-hosted
- [ ] TTS : OpenAI `tts-1` ou Eleven Labs (qualité voix française)
- [ ] Vision : modèles multimodaux (GPT-4o, Gemini 1.5 Pro, Claude Sonnet)
- [ ] Flutter : `record`, `image_picker`, `file_picker`
- [ ] Permissions iOS/Android (Info.plist `NSMicrophoneUsageDescription`, AndroidManifest)
- [ ] C2PA standard (lib `c2pa-python`)

### Tâches
- [ ] **Backend** `POST /voice/transcribe` (Whisper, job arq si > 1 min)
- [ ] **Backend** `POST /voice/synthesize` (audio MP3 stream ou URL signée)
- [ ] **Backend** `/chat/stream` accepte `images: [{base64|url}]`
- [ ] **Backend** `POST /files/upload` (validation MIME, taille max, ClamAV optionnel)
- [ ] **Backend** Service `image/generate` watermark C2PA + métadonnées XMP
- [ ] **Frontend** Bouton micro InputBar (enregistre → transcrit → user valide)
- [ ] **Frontend** Bouton speaker sur chaque réponse IA (TTS)
- [ ] **Frontend** Bouton appareil photo (vision)
- [ ] **Frontend** Bouton trombone (fichiers, RAG si > 1 page)
- [ ] **Frontend** Banner "Image générée par IA" sur images générées

### Livrable
- [ ] Démo : photo plat → "qu'est-ce que c'est" → réponse + lecture audio
- [ ] Image générée → watermark C2PA vérifié via `c2patool`

### Risques
- Coût vision ~10× texte → quota strict free.
- Latence STT longue → spinner + cancel.

---

# PHASE 8 — Planner + Notifications + Deep linking (M)

**Statut global :** [ ]

**Objectif.** L'IA aide à planifier ; tâches notifient via push, notifications ouvrent la bonne vue.

### Pré-requis à apprendre
- [ ] Function calling / tool use LLM
- [ ] FCM (Android) + APNs (iOS via FCM)
- [ ] Time zones (stocker UTC, `flutter_native_timezone`)
- [ ] Deep linking dans payload notification

### Tâches
- [ ] **Backend** Tables `tasks`, `events`
- [ ] **Backend** Tools LLM : `create_task`, `list_tasks`, `update_task`
- [ ] **Backend** Worker arq toutes les 5 min → tâches dues 10 min suivantes → push FCM
- [ ] **Backend** Stockage `device_tokens` (user_id, token, platform, last_seen)
- [ ] **Backend** Payload notification inclut `deep_link: nexya://task/{id}`
- [ ] **Frontend** `firebase_messaging` setup Android + iOS
- [ ] **Frontend** `PlannerScreen` (jour/semaine/mois) + création manuelle
- [ ] **Frontend** Demande permission notif AVEC écran d'explication AVANT prompt système
- [ ] **Frontend** Handler notification → router vers `/task/{id}`

### Livrable
- [ ] "Rappelle-moi maths demain 18h" → notification 18h00 lendemain → clic ouvre la tâche

### Risques
- Apple Developer payant 99 $/an pour APNs — anticiper.

---

# PHASE 9 — Modes Experts (RAG + collecte données) (L)

**Statut global :** [ ]

**Objectif.** Spécialiser NEXYA en 7+ experts avec qualité supérieure aux LLM généralistes.

### Pré-requis à apprendre
- [ ] Distinction prompt engineering / RAG / fine-tuning
- [ ] Web scraping respectueux (robots.txt, rate-limit, User-Agent)
- [ ] Datasets ouverts : HF Datasets, Kaggle, Common Crawl
- [ ] Licences : CC-BY-SA, MIT — savoir ce que tu peux redistribuer

### Tâches par expert

#### Expert Langues
- [ ] System prompt dédié
- [ ] Collecter corpus parallèles (Tatoeba, OPUS), dialogues, conjugaisons
- [ ] Indexer pgvector (`expert_slug='langues'`)
- [ ] Évaluation 30 questions blind test

#### Expert Cuisine
- [ ] System prompt dédié
- [ ] Collecter recettes camerounaises/africaines (sources licites)
- [ ] Indexer pgvector
- [ ] Évaluation 30 questions

#### Expert Studio créatif
- [ ] System prompt + collecte prompts d'image (PromptHero) + théorie design
- [ ] Indexer pgvector
- [ ] Évaluation

#### Expert Ingénierie
- [ ] System prompt + normes ISO publiques + formules
- [ ] Indexer pgvector
- [ ] Évaluation

#### Expert Productivité
- [ ] System prompt + GTD/Pomodoro/OKR
- [ ] Indexer pgvector
- [ ] Évaluation

#### Expert Informatique
- [ ] System prompt + docs officielles (Python, Flutter, Rust…)
- [ ] Indexer pgvector
- [ ] Évaluation

#### Expert Sciences/Maths
- [ ] System prompt + Khan Academy + Wikipedia formules + exercices
- [ ] Indexer pgvector
- [ ] Évaluation

### Tâches transversales
- [ ] **Backend** `/chat/stream` accepte `expert_slug`, charge system prompt + retrieve corpus
- [ ] **Frontend** Sélecteur expert header (étendre `NxModelPill`)

### Livrable
- [ ] "Cuisine" + "comment faire un ndolè" → réponse spécifique camerounaise avec sources, supérieure à généraliste

### Risques
- Tentation scraper sans permission → privilégier données licence claire.
- **Avancer expert par expert**, livrer chacun avant le suivant.

---

# PHASE 10 — Modèles locaux Gemma + fine-tuning + MLOps (XL)

**Statut global :** [ ]

**Objectif.** Réduire dépendance APIs propriétaires. Pipeline MLOps complet (versioning, registry, évals CI, drift, red-teaming).

### Pré-requis à apprendre
- [ ] Bases ML : forward pass, gradient descent, loss, overfitting (fast.ai partie 1, 8h)
- [ ] Architecture Transformer (3Blue1Brown + Jay Alammar)
- [ ] PyTorch basics (tutoriel 60 min)
- [ ] HuggingFace : `transformers`, `datasets`, `peft`, `trl`, `accelerate`
- [ ] PEFT/LoRA : fine-tuning paramétrique
- [ ] Quantization : GGUF, llama.cpp, AWQ
- [ ] MLflow ou HF Hub privé (model registry)
- [ ] Évaluation : perplexité, MMLU, red-teaming

### Tâches
- [ ] Choisir base : Gemma 2 9B ou Mistral 7B Instruct
- [ ] Télécharger HuggingFace, valider local (`transformers` ou Ollama)
- [ ] Préparer dataset fine-tuning ChatML JSONL (~10k-50k exemples)
- [ ] Versionner datasets DVC (push S3/MinIO)
- [ ] Fine-tune LoRA Colab Pro+ (A100, ~10 $/run)
- [ ] Suite évaluation CI (100-500 prompts, bloque déploiement si régression)
- [ ] Red-teaming (50 prompts adversariaux)
- [ ] Quantifier GGUF Q4_K_M
- [ ] Déployer Ollama VPS GPU (Hetzner ~150 €/mois) ou vLLM RunPod
- [ ] Enregistrer modèle dans registry (HF Hub privé ou MLflow)
- [ ] Ajouter `LocalProvider` couche IA Phase 2
- [ ] Drift detection (50 prompts canaris hebdo)
- [ ] Mode offline mobile optionnel : Gemma 2B quantifiée via `flutter_gemma`

### Livrable
- [ ] Modèle `nexya-cuisine-v1` bat GPT-4o-mini sur 50 questions cuisine camerounaise
- [ ] Hébergé infra dédiée, accessible router
- [ ] Suite évals + red-team verts

### Risques
- **Phase la plus exigeante.** Ne pas démarrer avant fast.ai partie 1 finie.
- Coût GPU — démarrer Colab gratuit.
- APK > 500 Mo pour mobile-embarqué — réserver Pro avec téléchargement.

---

# PHASE 11 — Subscriptions + Paiements multi-pays (M)

**Statut global :** [ ]

**Objectif.** Convertir free → payants ; encaisser légalement Cameroun, Afrique, international.

### Pré-requis à apprendre
- [ ] Stores Apple iAP + Google Play Billing (commission 15-30 %)
- [ ] RevenueCat (gratuit jusqu'à 2.5k $/mois)
- [ ] CinetPay (Mobile Money Cameroun + autres)
- [ ] Wave (Sénégal/CI), M-Pesa (Kenya), Moov Money
- [ ] Stripe (carte internationale web)
- [ ] Webhooks : signature, idempotence, retry
- [ ] Pricing par PPP (parité pouvoir d'achat)

### Tâches
- [ ] **Backend** Table `subscriptions`
- [ ] **Backend** `/webhooks/revenuecat` (HMAC) → met à jour `users.plan`
- [ ] **Backend** `/webhooks/cinetpay`
- [ ] **Backend** `/webhooks/stripe`
- [ ] **Backend** `/webhooks/wave` (selon expansion)
- [ ] **Backend** `/webhooks/mpesa` (selon expansion)
- [ ] **Backend** Job arq daily : downgrade users `plan_expires_at < now()`
- [ ] **Backend** Table `plans` (prix par pays)
- [ ] **Backend** Quotas custom par plan + middleware refus 402
- [ ] **Frontend** Intégrer `purchases_flutter` (RevenueCat SDK)
- [ ] **Frontend** `PaywallScreen` (bénéfices, prix adapté pays, bouton Restore Purchases obligatoire Apple)
- [ ] **Frontend** Affichage quotas restants Settings
- [ ] **Frontend** Détection pays (geo IP) → propose méthode paiement adaptée

### Livrable
- [ ] Sandbox Apple iAP → backend webhook → `users.plan = 'pro'` → quotas débloqués
- [ ] Test Mobile Money Orange Cameroun (compte réel test)

### Risques
- Refus Apple si paywall confus / restore absent — étudier HIG.
- Mobile Money très spécifique pays — tester comptes réels.

---

# PHASE 12 — Settings, RGPD, CGU, i18n, Accessibilité, AI Act (M)

**Statut global :** [ ]

**Objectif.** Tout ce qui rend l'app **publiable légalement** et utilisable par tous (handicap inclus).

### Pré-requis à apprendre
- [ ] RGPD pratique : guide CNIL "RGPD pour développeurs"
- [ ] Loi camerounaise n° 010/2010
- [ ] EU AI Act : obligations transparence, étiquetage, registre
- [ ] WCAG 2.1 AA : contraste 4.5:1, lecteurs écran, clavier

### Tâches
- [ ] **Backend** `GET /users/me/export` (ZIP toutes données JSON)
- [ ] **Backend** `DELETE /users/me` (soft-delete + purge arq après 30j)
- [ ] **Backend** Table `consent_log` (preuve RGPD)
- [ ] **Backend** Registre interne IA (AI Act) — doc modèles + données entraînement + évals risques
- [ ] **Frontend** SettingsScreen (profil, plan, langue, thème, notif, mémoire, exports, suppression, version, mentions légales, signaler bug)
- [ ] **Frontend** Pages CGU, CGV, Privacy, Mentions légales (Markdown + URL miroir)
- [ ] **Frontend** Bandeau consentement premier lancement (analytics + push) opt-in granulaire
- [ ] **Frontend** Étiquette "Powered by AI" visible en permanence (AI Act)
- [ ] **Frontend** Centre d'aide (FAQ + bouton support → email + Crisp widget)
- [ ] **Frontend** Traduction EN
- [ ] **Frontend** Traduction anglais Nigeria
- [ ] **Frontend** Traduction swahili
- [ ] **Frontend** Traduction wolof
- [ ] **Frontend** Traduction lingala
- [ ] **Frontend** Traduction bambara
- [ ] **Frontend** Traduction arabe Maghreb (RTL)
- [ ] **Frontend** Accessibilité WCAG : contraste audité (Stark Figma), `Semantics` partout, navigation clavier
- [ ] **Frontend** Dynamic type, mode contraste élevé
- [ ] **Frontend** Police OpenDyslexic optionnelle
- [ ] **Frontend** Test chaque écran VoiceOver iOS + TalkBack Android

### Livrable
- [ ] Export complet données fonctionne
- [ ] Suppression compte OK
- [ ] CGU lisibles in-app
- [ ] Switch EN + swahili OK
- [ ] App navigable au lecteur d'écran de bout en bout

### Risques
- CGU mal rédigées = blocage Apple/Google. Investir 200-500 € en revue juridique.

---

# PHASE 13 — Observabilité prod + Analytics produit (M)

**Statut global :** [ ]

**Objectif.** Visibilité technique (latence, erreurs, coûts) ET business (rétention, conversion, NPS).

### Pré-requis à apprendre
- [ ] OpenTelemetry : traces, spans, exporters OTLP
- [ ] Sentry SDK Python + Flutter
- [ ] Grafana + Prometheus
- [ ] Loki ou ElasticSearch (logs)
- [ ] PostHog (gratuit self-hosted) ou Mixpanel
- [ ] Feature flags : GrowthBook (gratuit) ou PostHog Experiments
- [ ] A/B testing bases (taille échantillon, significativité)
- [ ] BetterStack ou Instatus (status page)

### Tâches
- [ ] **Backend** OTel (`opentelemetry-instrumentation-fastapi`) → Tempo ou Honeycomb
- [ ] **Backend** Sentry SDK + scrubbing PII
- [ ] **Backend** Dashboards Grafana (latence par route, taux 5xx, coût IA jour/mois, RPS par plan)
- [ ] **Backend** Alertes (5xx > 1 % sur 5 min, coût IA seuil, refresh cleanup échec, drift modèle)
- [ ] **Frontend** `sentry_flutter` capture crashes
- [ ] **Frontend** PostHog SDK (signup, conversation_started, paywall_view, purchase, feature_used:vision/voice…) — anonymisé
- [ ] **Frontend** In-app NPS survey après 10 sessions positives
- [ ] **Frontend** GrowthBook feature flags wrapper
- [ ] **Infra** Status page publique `status.nexya.ai` (BetterStack)
- [ ] **Backend** Endpoint `/internal/metrics` admin (DAU, conversions, revenus)

### Livrable
- [ ] Tableau de bord unique répondant : actifs, dépensé IA, crash, latence p95, conversion paywall, NPS hebdo

### Risques
- Sur-instrumentation = bruit + coût. Démarrer 5 dashboards / 5 alertes max.

---

# PHASE 14 — CI/CD + Staging + Production + Sécurité périmètre (M)

**Statut global :** [ ]

**Objectif.** Tout commit `main` peut atteindre prod via chaîne automatisée. Périmètre prod sécurisé.

### Pré-requis à apprendre
- [ ] GitHub Actions (workflows, secrets, environments approbation)
- [ ] Docker registry (GitHub Container Registry)
- [ ] Reverse proxy + TLS (Caddy ou Traefik)
- [ ] Migrations alembic en prod (verrou + rollback)
- [ ] Backup PG : `pg_basebackup`, WAL-G vers S3
- [ ] Cloudflare : WAF, anti-DDoS, bot management
- [ ] DR : RTO/RPO, tests restore

### Tâches
- [ ] Workflow CI : lint → tests → build → push → deploy staging → smoke tests → approval → deploy prod
- [ ] Provisionner staging Hetzner CX32 + Caddy + docker compose
- [ ] Provisionner prod Hetzner CX42/CCX13 + Caddy + docker compose, domaine `api.nexya.ai`
- [ ] Cloudflare devant API : DNS, TLS, WAF OWASP, rate limit IP, bot fight, anti-DDoS L7
- [ ] Migrations alembic exécutées par script avant rolling restart
- [ ] Backup quotidien PG → S3/MinIO chiffré + WAL streaming continu
- [ ] **Test restore mensuel automatique** (sans test = backup inexistant)
- [ ] Versioning API : tout sous `/v1/` + politique deprecation 6 mois min
- [ ] Endpoint `/version/min-supported` pour forced-update mobile
- [ ] Workflow Flutter : `flutter analyze` + `flutter test` + golden tests sur PR
- [ ] **Runbook incidents** Markdown dans repo
- [ ] **Règles on-call solo** documentées (8h-22h CET hors P0)
- [ ] **Postmortem template** pour incidents > 30 min

### Livrable
- [ ] Push `main` → 12 min plus tard prod, monitoré, rollback en 1 commande
- [ ] Backup restauré avec succès en < 30 min
- [ ] Cloudflare bloque attaque test

### Risques
- Secrets en clair Actions → Environments + secrets chiffrés OIDC.
- Lock alembic prod → migrations en plusieurs étapes (add nullable → backfill → set not null).

---

# PHASE 15 — Play Store + App Store + Hardening mobile (M)

**Statut global :** [ ]

**Objectif.** App téléchargeable publiquement, hardenée contre reverse engineering, conforme politiques stores.

### Pré-requis à apprendre
- [ ] Apple Developer 99 $/an, Google Play 25 $ unique
- [ ] Signing : keystore Android + clés Apple App Store Connect
- [ ] Politiques Apple §5 + Google Play
- [ ] ASO bases (nom, mots-clés, screenshots)
- [ ] Obfuscation Flutter (`--obfuscate --split-debug-info`)
- [ ] ProGuard/R8 Android
- [ ] Certificate pinning client (`dio_certificate_pinning` ou pin SHA256 manuel)
- [ ] Détection root/jailbreak (`flutter_jailbreak_detection`)

### Tâches
- [ ] Créer comptes développeur, vérifier identité société (Phase 0)
- [ ] Générer keystore Android + **sauvegarder hors machine**
- [ ] Signing Gradle dans `android/app/build.gradle.kts`
- [ ] iOS : certificats + provisioning automatiques Xcode
- [ ] Builds release obfusqués (`flutter build apk --release --obfuscate --split-debug-info=build/symbols`)
- [ ] Certificate pinning (pin SHA256 cert prod dans le code)
- [ ] Détection jailbreak/root (refus opérations sensibles)
- [ ] Privacy Manifest iOS révision finale
- [ ] Listings : icône 1024×1024, screenshots iPhone 6.7" + Android phone+tablet, vidéo promo
- [ ] Description longue + courte FR + EN
- [ ] Politique confidentialité hébergée URL publique
- [ ] **Support tablette/iPad** : layout responsive (breakpoints 600 + 900 dp)
- [ ] **Performance budget** : startup < 2 s, jank < 1 %, APK < 50 Mo (App Bundle + slicing)
- [ ] Internal test → Closed beta TestFlight + Play Internal Testing (10-50 testeurs)
- [ ] Compte démo `apple-review@nexya.ai` peuplé pour reviewers
- [ ] Soumettre review (1-7 jours Apple, 1-3 jours Google)
- [ ] Fixer rejets éventuels (souvent 1ʳᵉ soumission rejetée — normal)
- [ ] Release par étapes : 1 % → 10 % → 50 % → 100 %

### Livrable
- [ ] App téléchargeable iOS + Android dans France + Cameroun + 5 pays africains francophones
- [ ] APK reverse-engineered ne révèle pas la logique métier

### Risques
- Rejets fréquents : permissions non justifiées, IAP manquant, métadonnées trompeuses, compte test reviewer absent.

---

# PHASE 16 — Bêta publique, marketing, itération (continu)

**Statut global :** [ ]

**Objectif.** Premiers utilisateurs réels valident le produit. Mesurer rétention J7/J30, ajuster.

### Pré-requis à apprendre
- [ ] Analytics produit (cohortes, funnel signup → engagement → paid)
- [ ] Bases growth : referrals, partage natif, content marketing
- [ ] ASO continu

### Tâches
- [ ] Soft launch : campus Cameroun, communautés Discord/WhatsApp tech
- [ ] Suivi métriques : DAU/WAU/MAU, conversion paywall, ARPU, churn, NPS
- [ ] Loop bug → roadmap → release toutes les 2 semaines
- [ ] Support utilisateur réactif (< 24 h)
- [ ] Demande avis store après 5 sessions positives (`in_app_review`)
- [ ] A/B test paywall copy
- [ ] A/B test prix mensuel vs annuel mis en avant
- [ ] A/B test onboarding 3 vs 4 étapes

### Livrable
- [ ] Premier mois : 1000 inscrits, 3-5 % conversion, < 2 % crash rate

### Risques
- Sur-engineering avant feedback. **Couper toute feature non utilisée après 1 mois mesuré.**

---

# PHASE 17 — Marketing, Brand, Growth (L, peut démarrer dès Phase 14)

**Statut global :** [ ]

**Objectif.** NEXYA a une identité visuelle pro, une présence en ligne, une stratégie growth scalable.

### Pré-requis à apprendre
- [ ] Bases brand (couleurs, typo, ton de marque)
- [ ] SEO : Search Console, mots-clés, contenu pilier
- [ ] Algorithmes TikTok / Instagram / X
- [ ] Email marketing (séquences onboarding, réactivation)

### Tâches
- [ ] **Identité visuelle complète** (logo pro Fiverr/99designs ~150-500 €)
- [ ] Charte graphique (couleurs, typo, espacements)
- [ ] App icon iOS/Android variantes
- [ ] Favicons
- [ ] Kit presse PDF
- [ ] **Landing site nexya.ai** (Astro ou Next.js + i18n FR/EN)
- [ ] Sections landing : hero, démo vidéo, fonctionnalités, pricing, FAQ, blog, CGU/Privacy
- [ ] Déploiement Vercel/Cloudflare Pages
- [ ] **Blog SEO** : 10 articles piliers
- [ ] Article "Apprendre l'anglais avec l'IA"
- [ ] Article "Recettes traditionnelles camerounaises"
- [ ] Article "Réviser le bac avec une IA"
- [ ] Article "5 utilisations de NEXYA pour étudiants Cameroun"
- [ ] Article "Pourquoi une IA Made in Africa"
- [ ] Article "Mode hors-ligne et économie de données"
- [ ] Article "Mémoire IA : confidentialité expliquée"
- [ ] Article "Comparaison NEXYA vs ChatGPT"
- [ ] Article "Cuisine africaine avec NEXYA Cuisine"
- [ ] Article "Maths avec NEXYA Sciences"
- [ ] **Réseaux sociaux** TikTok actif (3 posts/sem)
- [ ] Instagram actif
- [ ] X actif
- [ ] LinkedIn actif (B2B)
- [ ] Vidéos démo courtes (15-60 s) TikTok/Reels — minimum 10
- [ ] **Programme parrainage** ("invite 3 amis = 1 mois Pro")
- [ ] Tracking referral via deep links + RevenueCat promo offers
- [ ] **Communauté Discord** créée + modérée
- [ ] Communauté WhatsApp/Telegram alternative
- [ ] **5-10 micro-influenceurs Cameroun** identifiés
- [ ] Packs gratuits Pro envoyés aux influenceurs
- [ ] **Email marketing** Brevo/Loops compte créé
- [ ] Séquence onboarding 7 emails (J0, J1, J3, J7, J14, J21, J30)
- [ ] Séquence réactivation churn
- [ ] **Press kit** finalisé
- [ ] Outreach 3-5 médias tech africains (Le Bled' Parle Tech, AFRIK 21, Cio Mag)

### Livrable
- [ ] Landing live
- [ ] Brand cohérent
- [ ] 3 réseaux actifs avec >500 abonnés
- [ ] Blog 10 articles indexés Google
- [ ] Communauté Discord 100+ membres
- [ ] 5 partenariats influenceurs

### Risques
- Marketing chronophage qui détourne du produit. **Bloquer 1 jour/semaine max** sur marketing avant 1000 users payants.

---

# PHASE 18 — Support utilisateur & Opérations à l'échelle (M, démarre Phase 16)

**Statut global :** [ ]

**Objectif.** Helpdesk structuré, SLA respecté, recherche utilisateur continue, postmortems systématiques.

### Pré-requis à apprendre
- [ ] Outils helpdesk (Crisp gratuit, Intercom, Zendesk)
- [ ] SLA : définition cibles temps réponse, résolution
- [ ] Recherche utilisateur : interviews semi-structurées, méthode Nielsen 5-test
- [ ] Postmortems blameless (modèle Google SRE)

### Tâches
- [ ] Intégrer Crisp widget in-app (gratuit)
- [ ] Email `support@nexya.ai` actif
- [ ] SLA publié dans CGU
- [ ] **Base de connaissance** publique (Notion ou Crisp Helpdesk)
- [ ] 30 articles FAQ rédigés
- [ ] Macros réponses fréquentes (mot de passe oublié, bug streaming, paiement échoué)
- [ ] Tickets prioritaires : escalation auto si plan Pro ou bug paiement
- [ ] Status page mise à jour manuellement lors d'incidents
- [ ] **Postmortems** : doc Markdown obligatoire pour incidents > 30 min ou impact > 50 users
- [ ] **Recherche utilisateur** mensuelle : 5 interviews 15 min (Calendly + Zoom)
- [ ] Synthèse interviews dans Notion
- [ ] NPS in-app + relance email aux détracteurs (≤6)
- [ ] **Helpdesk metrics** : temps réponse moyen, CSAT post-ticket, % résolu 1ʳᵉ réponse

### Livrable
- [ ] Crisp opérationnel
- [ ] FAQ live
- [ ] 30 tickets traités SLA respecté
- [ ] 5 interviews users effectuées + synthèse
- [ ] 1 postmortem incident publié

### Risques
- Saturation support solo. À 500+ users payants → recruter freelance support 10h/semaine.

---

# PHASE 19 — Pentest, conformité avancée, scaling infra (L)

**Statut global :** [ ]

**Objectif.** Avant 10k users : valider sécurité par audit externe, conformité RGPD/AI Act, infra prête à scaler.

### Pré-requis à apprendre
- [ ] OWASP MASVS (mobile) + OWASP Top 10 (web/API)
- [ ] Bug bounty : programme `security@nexya.ai` + politique divulgation
- [ ] DPO : seuils de désignation obligatoire
- [ ] Multi-region PostgreSQL : réplica lecture, failover
- [ ] CDN images : Cloudflare R2, Bunny.net
- [ ] Charts Helm (si bascule K8s)

### Tâches
- [ ] **Pentest externe** freelance (Yes We Hack, Yogosha, ~1500-3000 €)
- [ ] Couvrir backend + mobile
- [ ] Corriger toutes failles critiques + hautes
- [ ] **Programme bug bounty informel** (page `/security` + email + remerciements + récompenses symboliques)
- [ ] **Audit RGPD** : DPIA si données sensibles
- [ ] Désigner DPO si seuils atteints
- [ ] **Audit AI Act** : registre interne complet, documentation modèles, atténuation risques
- [ ] **Audit accessibilité WCAG 2.1 AA externe** (Tanaguru, Atalan)
- [ ] **Scaling DB** : Hetzner CCX23 (16 vCPU 64 Go) + replica lecture asynchrone
- [ ] **CDN images** : MinIO/R2 derrière Cloudflare, signed URLs courte durée
- [ ] Multi-region (optionnel à 50k+ users) : staging EU + replica Africa
- [ ] **Cellule de crise** documentée Notion ("que faire si data breach", délai notif CNIL 72h, com users)

### Livrable
- [ ] Rapport pentest avec failles critiques/hautes corrigées
- [ ] DPIA documentée
- [ ] Audit AI Act validé
- [ ] Infra scalée jusqu'à 10k users sans incident

### Risques
- Découverte faille critique tard = stress + interruption. Pentest tôt.

---

# PHASE 20 — Levée / cession / scaling business (XL, optionnelle)

**Statut global :** [ ]

**Objectif.** Si rétention + revenus solides → ouvrir capital ou structurer cession. Sinon → optimiser rentabilité solo.

### Pré-requis à apprendre
- [ ] Pitch deck (modèle Sequoia, Y Combinator, 10-15 slides)
- [ ] Business plan financier 36 mois
- [ ] Cap table (Carta, Pulley, ou simple Sheets)
- [ ] Due diligence : checklist juridique + technique + financière
- [ ] Term sheets : SAFE, equity, dilution, liquidation preferences

### Tâches
- [ ] **Pitch deck** (problème, solution, marché TAM/SAM/SOM, traction, business model, équipe, ask)
- [ ] **Business plan financier** 36 mois (revenus projetés, coûts, équilibre)
- [ ] **Cap table propre** (même 100 % toi)
- [ ] **Due diligence ready** : code propre commenté, tests, docs architecture, conformité, contrats, comptabilité à jour
- [ ] **Identifier accélérateurs Afrique** : MEST, Founders Factory Africa, Antler, Orange Digital Center Cameroun
- [ ] **Fonds early-stage Afrique** : Partech Africa, Ventures Platform, Future Africa, AFK Foundation
- [ ] **Recrutement éventuel** (cofondateur tech ou growth, premier dev junior, designer)

### Livrable
- [ ] Pitch deck + BP + cap table + DD pack prêts
- [ ] Si décision lever : 5 RDV investisseurs

### Risques
- Lever trop tôt = dilution sans levier.
- Lever trop tard = épuisement perso.
- Fenêtre optimale = traction prouvée + besoin clair de capital.

---

# 📚 SECTION FINALE — Apprentissage transversal

À travailler **en parallèle** des phases qui les exigent. Estimations = autonomie, pas expertise.

## Backend / Python
- [ ] Python async + asyncio (Real Python + Edgar Roman) — 12 h — avant Phase 1
- [ ] FastAPI (doc Tutorial + Advanced) — 15 h — avant Phase 1
- [ ] SQLAlchemy 2.0 async (doc Unified Tutorial + Asyncio) — 10 h — avant Phase 4
- [ ] PostgreSQL avancé ("Use the Index, Luke!") — 15 h — avant Phase 5
- [ ] pgvector + embeddings (Supabase blog + Sentence-BERT paper) — 8 h — avant Phase 6

## Frontend / Flutter
- [ ] Riverpod 3.1 (doc + Code with Andrea ~30 €) — 12 h — avant Phase 3
- [ ] Architecture Flutter Clean/Repository (Reso Coder + Andrea) — 15 h — avant Phase 4
- [ ] Tests Flutter widget + integration (doc + Andrea) — 10 h — avant Phase 14

## IA / ML
- [ ] Bases ML (fast.ai partie 1) — 30-40 h — avant Phase 9-10
- [ ] NLP / Transformers (HF NLP Course + Illustrated Transformer) — 25 h — avant Phase 10
- [ ] Fine-tuning LoRA (HF PEFT docs + Maxime Labonne) — 15 h — avant Phase 10
- [ ] MLOps (Made With ML, Goku Mohandas) — 20 h — avant Phase 10

## DevOps / Sécurité
- [ ] Docker / Linux (Docker Deep Dive + Linux Command Line) — 20 h — avant Phase 14
- [ ] CI/CD GitHub Actions (doc + Manning book) — 8 h — avant Phase 14
- [ ] OpenTelemetry + Grafana (Honeycomb + Grafana University) — 10 h — avant Phase 13
- [ ] Sécurité mobile (OWASP MASVS) — 8 h — avant Phase 15
- [ ] Sécurité backend (OWASP Top 10 + PortSwigger Academy) — 15 h — avant Phase 19

## Business / Légal / Marketing
- [ ] In-App Purchases + RevenueCat (RevenueCat docs + Apple IAP by Tutorials) — 12 h — avant Phase 11
- [ ] CinetPay + Mobile Money (doc + sandbox) — 6 h — avant Phase 11
- [ ] RGPD pratique + EU AI Act (CNIL + AI Act résumé) — 12 h — avant Phase 12
- [ ] Loi camerounaise 010/2010 (texte officiel + cabinet local) — 4 h — avant Phase 12
- [ ] Accessibilité WCAG 2.1 (A11y Project + WebAIM) — 10 h — avant Phase 12
- [ ] Apple HIG + Google Play Policy — 10 h — avant Phase 15
- [ ] Brand & design ("Refactoring UI" Adam Wathan) — 15 h — avant Phase 17
- [ ] SEO + content marketing (Ahrefs Academy + Animalz) — 12 h — avant Phase 17
- [ ] Réseaux sociaux organiques (Justin Welsh OS, Buffer blog) — 8 h — avant Phase 17
- [ ] Email marketing (Brevo Academy + Justin Welsh) — 6 h — avant Phase 17
- [ ] Recherche utilisateur ("The Mom Test" Rob Fitzpatrick + NN/g) — 8 h — avant Phase 18
- [ ] Postmortems & SRE (Google SRE Book chap. 15) — 4 h — avant Phase 18
- [ ] Comptabilité / juridique entreprise Cameroun (CFCE + expert-comptable) — 8 h — avant Phase 0
- [ ] Pitch / fundraising (YC Startup School + Sequoia deck) — 15 h — avant Phase 20

**Total minimum : ~360 h ≈ 9-10 semaines à plein temps**, étalées intelligemment.

---

# 🎯 Prochain pas concret immédiat

- [ ] Phase 0 : réserver `nexya.ai`, `nexya.com`, `nexya.cm` dans la semaine
- [ ] Phase 1 : démarrer `POST /auth/forgot-password` avec compte SendGrid gratuit
- [ ] Apprentissage : lancer fast.ai partie 1 leçon 1 en parallèle

---

# 📝 Journal de progression

> Ajouter une ligne datée chaque vendredi avec : phase courante, % avancement, blocages, décisions.

- **2026-04-19** — Phase A frontend bouclée (secure_storage, api_client, auth_interceptor, retry_interceptor, chat_remote_datasource JWT, suppression LLM dead code) + seed.py backend + sync CLAUDE.md des deux côtés. État : ~13 % du produit final. Roadmap V2 finalisée et adoptée comme source de vérité.
- **2026-04-21** — **Phase 4 Chat persisté — Lot 1 (fondation data) livré ([ ] → [~]).** 4 fichiers créés (`app/features/chat/__init__.py`, `models.py` ~200 lignes, `schemas.py` ~210 lignes, `migrations/versions/002_create_chat_tables.py` ~125 lignes) + 1 modifié (`migrations/env.py`). 3 tables (`conversations` / `messages` / `abuse_reports`) avec soft-delete, dénormalisation compteurs, index composite cursor-stable, index partiel favoris, 6 CHECK constraints, UNIQUE anti-doublon abuse, FK cascade Postgres. 11 schémas Pydantic v2 avec compat descendante (`history=[...]` toléré jusqu'à migration Flutter). Vérifications automatiques : `py_compile` OK, `Base.metadata` enregistre les 3 tables + 5 indexes + 6 CHECK + 11 schémas validés. Reste Lots 2-5 (service, router, refactor `/chat/stream` persisté, worker auto-titre, `POST /reports`). Backend passe à ~41 % couvert.
- **2026-04-21** — **Couche IA backend Tier 1 (Phase 2 → [~])** — 7 briques livrées et intégrées : (1) ABC `LlmProvider` + types neutres + erreurs typées, (2) `GeminiProvider` réel (chat + Imagen 3) + 3 stubs (OpenAI / Anthropic / Qwen), (3) `LlmRouter` + 11 `ExpertConfig` (general + 10 experts), (4) `ModerationService` OpenAI fail-open, (5) `BudgetTracker` Redis (chat/img user/jour, IP burst, cap modèle, INCR + DECR rollback), (6) Retry exponentiel + jitter + `CircuitBreaker` `(provider, model)` (CLOSED / OPEN / HALF_OPEN), (7) `StreamHandler` SSE (heartbeat 15 s, annulation duale `is_disconnected()` + clé Redis, traversée chaîne fallback, disclaimer prefix) + Observabilité (`StreamMetrics` + table prix USD/1M tokens + log unique `ai.chat.completed`). Endpoints refactorés via Couche IA : `/chat/stream`, nouveau `/chat/stop`, `/image/generate`. 9/9 tests verts (zéro régression). État : ~17 % du produit final, IA / Modèles ~28 %, Backend ~38 %. Reste Phase 2 : câblage SDK réels OpenAI/Anthropic/Qwen, cache Redis prompt, RAG attribution sources, garde-fous métiers actifs côté modération.
- **2026-04-21** — **Phase 4 Chat persisté — Lot 3 (router CRUD) livré ([ ] → [x]).** 6 endpoints REST `/chat/conversations` : `POST` (201 + `ConversationResponse`), `GET` paginé (filtres `is_archived` / `is_favorite` / `cursor` / `limit` avec plafond 50), `GET {id}` (404 IDOR-safe), `PATCH {id}` (partiel via `model_dump(exclude_unset=True)`), `DELETE {id}` (soft → 204), `GET {id}/messages` (cursor ASC, owner check). `features/chat/router.py` (~180 lignes) enregistré dans `main.py`. Schéma `ConversationsPage` Pydantic ajouté à `schemas.py`. DTO ORM du service renommé `ConversationsPageOrm` pour cohérence avec `MessagesPageOrm` déjà présent. 16 tests `test_conversations_crud.py` via `TestClient` + `app.dependency_overrides` (`get_current_user` / `get_db`) + `AsyncMock` monkeypatché sur `ConversationService` : happy-path × 6, isolation cross-user 404 × 4, curseur forgé → 422 `VALIDATION_ERROR`, UUID malformé → 422 Pydantic, titre vide rejeté, `limit>50` rejeté. Suite complète : **32/32 tests verts** (9 auth + 7 service + 16 CRUD). Backend ~45 % couvert. Reste Lots 4-5 : refactor `/chat/stream` persisté (placeholder assistant + finalisation atomique), worker arq auto-titre + `POST /reports`.
- **2026-04-21** — **Phase 4 Chat persisté — Lot 4 (refactor `/chat/stream` persisté) livré ([ ] → [x]).** Architecture à 3 modes côté `features/chat/router.py` : (1) legacy stateless (`conversation_id=null` + `history=[...]`) — le stream passe, rien n'est écrit en base (rétrocompat Flutter) ; (2) nouvelle conversation persistée (`conversation_id=null`, pas d'`history`) — `ConversationService.ensure_conversation_for_stream()` crée la conv, `start_stream_turn()` insère user + placeholder assistant (`status='streaming'`) dans la même transaction + `_bump_counters(delta=2)` atomique ; (3) conv existante persistée — owner check 404 IDOR-safe + `load_context_messages(limit=30)` DESC→chronologique. Pipeline commun : budget → modération → `StreamHandler.stream()` → wrapper `_persisted_stream()` qui intercepte chaque événement SSE via `_observe_sse_event()` (parser custom event/data), accumule `content_parts`, mémorise `done_reason` / `error_code`, et en `finally` lance `asyncio.shield(_finalize_in_fresh_session(...))` pour finaliser même si le client se déconnecte. Finalisation atomique (`finalize_assistant_stream()`) : mapping `_DONE_REASON_TO_STATUS = {stop→completed, cancelled→cancelled, error→failed}`, UPDATE `Message` (content + tokens + `Decimal(str(cost_usd))` + status + `finished_at` + `provider` + `model` + `error_code`) + UPDATE `Conversation.last_message_at` dans un `AsyncSessionLocal()` neuf (découplage du lifecycle de la requête). `StreamContext` étendu d'un champ optionnel `metrics: StreamMetrics | None` pour que le router lise provider/model/usage/cost_usd sans modifier la sémantique `yield` du `StreamHandler`. Migration `/chat/stream` + `/chat/stop` de `main.py` vers `features/chat/router.py` + nouveau module `app/ai/runtime.py` (`get_ai_router()` / `get_stream_handler()` / `reset_runtime_for_tests()`) qui casse la circulaire `main.py ↔ chat_router`. Header `X-Conversation-Id` posé sur les modes persistés uniquement. 18 tests `tests/test_chat_stream_persisted.py` verts : parsing SSE (5), mapping statuts, `ensure_conversation_for_stream` × 2, `start_stream_turn`, `finalize_assistant_stream` × 3 (happy, invalid status, float→Decimal), `/chat/stop`, legacy stateless, persisted happy → `completed`, persisted error → `failed` + `error_code`, modération bloquée → 400, message vide → 422. **Suite complète : 50/50 tests verts**, zéro régression. Backend ~52 % couvert. Reste Lot 5 : worker arq auto-titre + `POST /reports` + rate limit abuse.
