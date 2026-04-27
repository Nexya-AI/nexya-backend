# Glossary — NEXYA Backend

Termes techniques + business utilisés dans la documentation NEXYA.

---

## Brand & Produit

- **NEXYA** — Nom du produit (assistant IA mobile + web).
- **Nexyalabs** — Société qui développe NEXYA (à formaliser SARL).
- **NYLI** — Nom de la personnalité IA conversationnelle de NEXYA
  (en cours d'évolution, pas encore stabilisé).
- **Studio** — Mode `expert_id="studio"` dédié génération d'images
  (Imagen 3). Pas de chat, image-only.
- **Expert** — Domaine de spécialisation IA (general / computer /
  science / finance / language / cooking / studio / engineering /
  productivity / medicine / legal). Le `expert_id` est le contrat
  API stable avec Flutter (cf. `EXPERT_REGISTRY`).
- **Africa-first** — Doctrine produit : chaque décision tient compte
  des contraintes 2G/3G + low-end devices + paiements Afrique.

## Architecture

- **LlmRouter** — Composant qui résout `expert_id` → triplet
  `(provider, model, ExpertConfig)`. Le backend décide toujours du
  modèle, jamais le frontend.
- **ExpertConfig** — Frozen dataclass définissant un expert :
  `system_prompt`, `primary_provider`, `primary_model`,
  `fallback_chain`, `temperature`, `tools_allowed`, etc.
- **Fallback chain** — Liste ordonnée de `(provider, model)` essayée
  par `StreamHandler` si le primaire échoue (5xx/timeout après retry).
- **StreamHandler** — Orchestrateur SSE avec heartbeat 15s + cancel
  duale (Redis key + HTTP disconnect) + traversée fallback chain.
- **CircuitBreaker** — État `CLOSED → OPEN → HALF_OPEN` par
  `(provider, model)`. Coupe les appels après 5 échecs / 30s.
- **RetryPolicy** — `max_attempts=3, base_delay=0.5, max_delay=5,
  jitter_ratio=0.25`. Retry uniquement avant le 1er chunk SSE.

## Persistance

- **`trace_id`** — UUID propagé via header `X-Request-ID`. Bind dans
  structlog → tous les logs HTTP corrélés.
- **`session_id`** — UUID d'un stream chat. Utilisé pour cancel via
  Redis key `chat:cancel:{session_id}` + dédup `ai_calls.session_id
  UNIQUE`.
- **`expert_id`** — Slug stable contrat Flutter
  (`general`/`computer`/...). Snake_case strict.
- **Soft-delete** — Pattern `deleted_at TIMESTAMPTZ NULL`. Index
  partiel `WHERE deleted_at IS NULL` pour hot path actif.
- **Cursor pagination** — Keyset `(created_at, id) DESC`,
  base64url-encoded `iso|uuid`. Évite `OFFSET` qui scale O(n).
- **`pgvector`** — Extension PostgreSQL pour embeddings vectoriels
  + recherche cosinus HNSW. Utilisé `memories` (1536 dim OpenAI) +
  `expert_corpus_chunks` (768 dim Gemini) + `document_chunks`.

## IA & Coûts

- **TTFT** (Time To First Token) — Latence entre envoi de la requête
  et réception du premier `event: chunk` SSE. Métrique critique UX
  Africa-first 2G/3G.
- **Mock-first** — Pattern signature NEXYA : si la clé API d'un SaaS
  est vide, un `Mock<X>Service` usurpe l'identité (8 services :
  Brevo, hCaptcha, FCM, Vision, Voice, Embeddings, Crisp, MinIO).
- **PromptCache** — Redis SHA-256 sur `(model, messages, system_prompt,
  temperature, max_tokens, expert_id)` TTL 24h. Skip safety-critical
  + multi-turn user.
- **TokenEstimator** — Cap pré-flight 30 000 tokens/requête via
  tiktoken `o200k_base` + heuristique Gemini/Anthropic.
- **BudgetTracker** — Quotas Redis user-scope par jour (chat, image,
  voice minutes, vision images, embeddings).
- **CostTracker** — Persiste chaque appel LLM dans `ai_calls` (forensic)
  + UPSERT `usage_daily` (agrégat) en fire-and-forget asyncio.

## Conformité & Sécurité

- **RGPD** — Règlement général sur la protection des données UE 2016/679.
  Articles 7 (consent), 15 (access), 17 (erasure), 20 (portability)
  implémentés en J1.
- **AI Act** — Règlement IA UE 2024/1689. Article 13 (transparency
  obligations) applicable août 2026. NEXYA implémente le registre
  `ai_calls` enrichi (`legal_basis`, `data_categories`,
  `retention_until`).
- **DPIA** — Data Protection Impact Assessment (Article 35 RGPD).
  Phase M3 avec consultant DPO externe.
- **DPO** — Data Protection Officer. V1 = Ivan en interne, V2 = DPO
  externe post 50k users.
- **DPA** — Data Processing Agreement (Article 28 RGPD). Voir
  [`compliance/dpa-template.md`](compliance/dpa-template.md).
- **STRIDE** — Threat model : Spoofing / Tampering / Repudiation /
  Information disclosure / Denial of service / Elevation of privilege.
- **OWASP Top 10** — Standard de référence vulnérabilités web.
  Mapping NEXYA dans
  [`compliance/security-checklist.md`](compliance/security-checklist.md).
- **HSTS preload** — `Strict-Transport-Security` avec `preload` →
  soumission [hstspreload.org](https://hstspreload.org/) →
  navigateurs hardcode la liste. Engagement long terme HTTPS.

## Africa-context

- **OHADA** — Organisation pour l'Harmonisation en Afrique du Droit
  des Affaires. 17 pays. Expert `legal` calibré sur OHADA + droit
  camerounais.
- **CEMAC** — Communauté Économique et Monétaire de l'Afrique
  Centrale. Zone FCFA / XAF.
- **BRVM** — Bourse Régionale des Valeurs Mobilières (Abidjan, UEMOA).
- **FCFA / XAF** — Franc CFA Afrique Centrale.
- **CinetPay** — Aggregateur paiements mobile money Afrique de l'Ouest
  (Orange Money, MTN, Wave, Moov).
- **NotchPay** — Alternative à CinetPay, Cameroun + 15 pays.
- **Mobile money** — Système de paiement par téléphone (vs carte
  bancaire). Dominant Afrique francophone.

## Stack technique

- **FastAPI** — Framework web Python async (Starlette + Pydantic v2).
- **SQLAlchemy 2.0 async** — ORM async + Alembic migrations.
- **Pydantic v2** — Validation/sérialisation Python typed.
- **arq** — Worker async Python (queue Redis + cron).
- **structlog** — Logger structuré JSON corrélé `trace_id`.
- **OpenTelemetry (OTel)** — Standard tracing distribué.
- **Sentry** — Error tracking + breadcrumbs.
- **Prometheus** — Time-series metrics scrape pull.
- **Grafana** — Visualisation dashboards.
- **k6** — Load testing JS-based moderne (vs JMeter, Locust).
- **MinIO** — Object storage compatible S3 (open-source, dev local).
- **psycopg v3** — Driver PostgreSQL async Python (vs asyncpg).

## Évals IA (N3)

- **Évals** — Tests qualité IA reproductibles. Détectent régressions
  introduites par PR (changement prompt, modèle, fallback, SDK).
- **MockJudge** — Juge déterministe SHA-256 pour test pipeline gratuit
  CI sans clé API.
- **GeminiJudge** — Juge sémantique réel via Gemini 2.5 Pro structured
  output. Coûte ~$30/mois nightly.
- **Baseline gelée** — Snapshot pass_rate + score_avg par catégorie
  committé `tests/evals/baselines/baseline.json`. Diff vs baseline =
  signal régression.
- **pp_drop** — Pourcentage points drop (95% → 85% = -10pp). Seuil
  régression PR=10pp / nightly=5pp.

## Workflows GitHub Actions

- **`ci.yml`** — Lint + typecheck + security + tests + docker-build +
  migrations-check (L1).
- **`release.yml`** — Tag semver → build image GHCR + GitHub Release
  notes (L1).
- **`evals.yml`** — Évals IA reproductibles (PR mock + nightly real)
  (N3).
- **`load.yml`** — k6 load tests (manual + weekly Sunday) (N4).
- **`dd-exports-fresh.yml`** — Vérifie freshness `openapi.json` +
  `schema.sql` sur push main (O2).
- **`codeql.yml`** — Security scan weekly (L1).
- **`dependabot-auto-merge.yml`** — Auto-merge patch/minor (L1).

## Phases du projet

- **Phase 1** — Auth (livré A1+A3).
- **Phase 2** — Couche IA Tier 1 (livré 7 briques + B1+B2+B3).
- **Phase 4** — Chat persisté (livré Lots 1-5 + F2.0).
- **Phase 5** — History + Projects + Library (livré C1+C2+C3).
- **Phase 6** — Memory + RAG (livré D1→D5).
- **Phase 7** — Voice + Vision + Files + Watermarking (livré E1→E4).
- **Phase 8** — Planner + Notifications (livré F1+F2+F3).
- **Phase 11** — Subscriptions paiements (TODO I1+I2+I3).
- **Phase 13** — Observabilité prod (livré K1+K2).
- **Phase 14** — CI/CD + RGPD (livré L1+J1).
- **Phase 18** — Crisp escalation + Helpdesk metrics (livré N4 volet B).
- **Phase 19** — Pentest M1 + DPIA M3 + Multi-region (TODO post-launch).
- **Bloc N** — Tests unitaires (N1+N2) + évals (N3) + load (N4) — ✅ 100%.
- **Bloc O** — OpenAPI (O1) + DD-ready polishing (O2) — ✅ 100%.
- **Bloc G** — RAG corpus experts (G1 langues abandonné post-blind-test,
  G2-G7 TODO).
- **Bloc H** — Fine-tuning Gemma langues camerounaises (TODO Phase 19).
- **Bloc L2/L3/L4** — Déploiement staging + prod + observabilité prod
  (TODO).
- **Bloc M1/M2/M3** — Pentest + DPIA + DD-ready polishing (M3 = O2 ✅).
