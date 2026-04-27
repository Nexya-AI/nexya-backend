# NEXYA Backend

[![CI](https://img.shields.io/badge/CI-passing-brightgreen)](.github/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12-blue)](pyproject.toml)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688)](pyproject.toml)
[![License](https://img.shields.io/badge/License-Proprietary-red)](docs/compliance/dpa-template.md)

> **Executive summary (EN).** NEXYA is a French-first, Africa-first AI
> assistant API. Backend stack: FastAPI (Python 3.12 async) +
> PostgreSQL 16 + Redis 7 + MinIO/S3 + arq workers. SSE-streamed chat
> with multi-expert routing across 4 LLM providers (Gemini, OpenAI,
> Anthropic, Qwen). RGPD UE 2016/679 + AI Act EU 2024/1689 compliant
> by design. Mock-first SaaS pattern (Brevo, hCaptcha, FCM, Vision,
> Voice, Embeddings, Crisp) — dev runs without any external API key.

---

## 🚀 Onboarding 5 minutes

> Tu arrives lundi 9h sur ce repo. À 9h05, tu dois avoir un
> `/healthz` qui répond en local. Si ce n'est pas le cas, **ce
> README a échoué** : ouvre une issue.

### Pré-requis
- Python **3.12+** (`python --version`)
- Docker Desktop installé et lancé (`docker --version`)
- `uv` (gestionnaire de packages rapide) : `pip install uv`
- bash (Linux/Mac/WSL/Git Bash sur Windows)

### Setup
```bash
# 1. Cloner + entrer dans le dossier
git clone <repo>
cd nexya_backend

# 2. Environnement Python
uv venv
source .venv/bin/activate          # Linux/Mac
.venv\Scripts\activate             # Windows
uv pip install -e ".[dev]"

# 3. Variables d'environnement (mock-first par défaut)
cp .env.example .env
# Ouvre .env et remplis au minimum :
#   APP_SECRET=<openssl rand -hex 32>
#   JWT_PRIVATE_KEY / JWT_PUBLIC_KEY (cf. ci-dessous)

# 4. Générer les clés JWT RS256
openssl genrsa -out private.pem 2048
openssl rsa -in private.pem -pubout -out public.pem
# Copier le contenu dans JWT_PRIVATE_KEY et JWT_PUBLIC_KEY du .env

# 5. Démarrer Postgres + Redis + MinIO
docker compose -f docker/docker-compose.yml up -d

# 6. Migrations DB
alembic upgrade head

# 7. Comptes démo (idempotent)
python -m scripts.seed_dev
# → free@nexya.ai / DemoFree2026!
# → pro@nexya.ai  / DemoPro2026!

# 8. Démarrer l'API
uvicorn app.main:app --reload --port 8000

# 9. Vérifier
curl http://localhost:8000/healthz
# {"success":true,"data":{"status":"ok",...}}

# 10. Documentation interactive
open http://localhost:8000/docs        # Swagger UI
open http://localhost:8000/redoc       # ReDoc
```

---

## 🗂️ Structure du repo

```
nexya_backend/
├── app/                     ← Code applicatif FastAPI
│   ├── main.py              ← Point d'entrée (lifespan + routers + middlewares)
│   ├── config.py            ← Settings Pydantic (env vars typées)
│   ├── core/                ← Infrastructure partagée
│   │   ├── auth/            ← JWT RS256 + guards + refresh rotation
│   │   ├── database/        ← AsyncEngine SQLAlchemy + Redis pool
│   │   ├── email/           ← Brevo + Mock + Jinja2 templates
│   │   ├── errors/          ← NexYaException hierarchy + handlers globaux
│   │   ├── health/          ← detect_version + ExtendedHealthService (O1)
│   │   ├── observability/   ← OTel + Sentry + Prometheus (K1)
│   │   ├── openapi/         ← OpenAPI customizer (O1)
│   │   ├── security/        ← Sanitizer + captcha + rate limiter + headers (O1)
│   │   └── storage/         ← MinIO/S3 + virus scanner + mime detector
│   ├── ai/                  ← Couche IA
│   │   ├── router.py        ← LlmRouter (expert_id → provider+model)
│   │   ├── experts.py       ← 11 ExpertConfig
│   │   ├── streaming.py     ← StreamHandler SSE + heartbeat + cancel
│   │   ├── providers/       ← Gemini, OpenAI, Anthropic, Qwen, OpenRouter, Mock
│   │   ├── embeddings/      ← OpenAI + Gemini + Mock
│   │   ├── voice/           ← Whisper STT + TTS providers
│   │   ├── vision/          ← Gemini Vision + GPT-4o + Mock
│   │   ├── tools/           ← Function calling registry + Planner tools
│   │   ├── fcm/             ← Firebase Cloud Messaging providers
│   │   ├── budget_tracker.py
│   │   ├── circuit_breaker.py
│   │   ├── retry.py
│   │   ├── cache.py
│   │   ├── moderation.py
│   │   ├── moderation_rules.py
│   │   ├── token_estimator.py
│   │   ├── observability.py
│   │   ├── cost_tracker.py
│   │   └── engine/          ← QueryEngine + SessionStore
│   ├── features/            ← Features verticales
│   │   ├── auth/            ← /auth/* + /user/*
│   │   ├── chat/            ← /chat/stream + conversations CRUD
│   │   ├── projects/
│   │   ├── library/
│   │   ├── files/
│   │   ├── voice/
│   │   ├── vision/
│   │   ├── memory/
│   │   ├── rag/
│   │   ├── planner/
│   │   ├── notifications/
│   │   ├── feedback/
│   │   ├── suggestions/
│   │   ├── ai_models/
│   │   ├── rgpd/
│   │   └── helpdesk/
│   ├── integrations/        ← Wrappers SaaS (Crisp, Gemini, etc.)
│   └── shared/              ← Schémas Pydantic partagés
├── workers/                 ← arq tâches async
│   ├── worker.py
│   ├── auth_tasks.py        ← cleanup_refresh_tokens
│   ├── chat_tasks.py        ← auto-titre conversation
│   ├── memory_tasks.py      ← extraction faits durables
│   ├── chunk_tasks.py       ← indexation RAG documents
│   ├── scheduler_tasks.py   ← Planner dispatch + execute
│   ├── ai_tasks.py          ← flush ai_sessions Redis → DB
│   └── rgpd_tasks.py        ← purge_deleted_accounts
├── migrations/              ← 19 migrations Alembic
├── tests/                   ← ~1700+ tests pytest
│   ├── load/                ← k6 load tests (N4)
│   └── evals/               ← Évals IA reproductibles (N3)
├── docker/                  ← Dockerfile multi-stage + docker-compose
├── grafana/                 ← Dashboards JSON + alerting (K2)
├── scripts/                 ← seed_dev, release, rollback, exports DD (O2)
└── docs/                    ← Documentation (O2 DD-ready)
    ├── architecture/        ← overview, data-model, request-flow, security, etc.
    ├── compliance/          ← rgpd, ai-act, security-checklist, dpa-template
    ├── api/                 ← endpoints, error-codes, versioning, openapi.json
    ├── runbooks/            ← incident-response, deployment-l2, db-restore
    ├── adr/                 ← Architecture Decision Records
    ├── glossary.md
    ├── ROADMAP.md
    └── BACKEND_SESSIONS_PLAN.md
```

---

## 🛠️ Commandes du quotidien

### Tests
```bash
make test                    # toute la suite
make test-fast               # skip @pytest.mark.live
make coverage                # avec rapport HTML
pytest tests/test_evals_judge.py -v   # un fichier précis
```

### Lint + format
```bash
make lint                    # ruff check + ruff format --check
make format                  # auto-fix
make typecheck               # mypy (relâché V1)
```

### Migrations DB
```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1
```

### Évals IA (N3)
```bash
python -m tests.evals --judge=mock --category=all     # gratuit, déterministe
python -m tests.evals --judge=gemini --category=safety # vrai juge (clé requise)
python -m tests.evals --update-baseline                # snapshot nouveau standard
```

### Load tests (N4)
```bash
bash tests/load/run.sh --scenario auth_burst
bash tests/load/run.sh                                 # tous les scénarios
```

### Exports DD (O2)
```bash
python -m scripts.export_openapi    # docs/api/openapi.json
bash scripts/export_schema.sh       # docs/architecture/schema.sql
make export-dd                      # les deux
```

### Worker arq (cron + jobs async)
```bash
arq workers.worker.WorkerSettings
```

---

## 📦 Tests & qualité

| Métrique | Valeur post-O2 |
|---|---|
| Tests pytest | ~1700+ verts |
| Catégories | unit + flow integration + load (k6) + IA evals |
| Couverture mypy | `app.*` `ignore_errors=true` V1 (TODO Phase 19) |
| Linting | ruff strict (B008, TC00x ignorés V1) |
| CI workflows | ci / release / codeql / dependabot / evals / load / dd-exports |
| Schéma OpenAPI | 60 endpoints + 20 tags + JWT BearerAuth |
| Migrations | 19 (auth → helpdesk) |

---

## 🔒 Production safety

L'API **refuse de démarrer** en `ENV=production` si :

- `ALLOWED_ORIGINS=*`
- `APP_SECRET` faible / défaut
- `JWT_PRIVATE_KEY` ou `JWT_PUBLIC_KEY` vides
- `DEBUG=true` ou `DB_ECHO=true`
- `PROMETHEUS_SCRAPE_TOKEN` vide (endpoint /metrics ouvert = fuite KPI)
- `GRAFANA_ADMIN_PASSWORD` vide ou `admin`
- `RGPD_ADMIN_EMAILS` vide (endpoint /rgpd/admin/* sans ACL)
- `SECURITY_HEADERS_PRESET` ∉ {`prod`, `off`} (preset `off` = kill-switch incident)

Ces garde-fous sont définis dans `app/config.py::Settings._enforce_production_safety`.

---

## 📚 Documentation Due Diligence

Pour un audit externe (investisseur / consultant CTO / nouveau Tech
Lead), tout est dans `docs/` :

| Fichier | Pour qui |
|---|---|
| [`docs/architecture/overview.md`](docs/architecture/overview.md) | Vue 30k pieds |
| [`docs/architecture/data-model.md`](docs/architecture/data-model.md) | DBA / data engineer |
| [`docs/architecture/request-flow.md`](docs/architecture/request-flow.md) | Backend dev / SRE |
| [`docs/architecture/ai-architecture.md`](docs/architecture/ai-architecture.md) | ML / IA engineer |
| [`docs/architecture/security-posture.md`](docs/architecture/security-posture.md) | Security audit |
| [`docs/architecture/observability.md`](docs/architecture/observability.md) | SRE / Ops |
| [`docs/compliance/rgpd.md`](docs/compliance/rgpd.md) | DPO / juriste |
| [`docs/compliance/ai-act.md`](docs/compliance/ai-act.md) | Compliance EU |
| [`docs/api/openapi.json`](docs/api/openapi.json) | Postman / Insomnia / SDK gen |
| [`docs/runbooks/`](docs/runbooks/) | Ops 3h du matin |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records |
| [`docs/glossary.md`](docs/glossary.md) | Lecteur étranger |

---

## 🌍 Africa-first

NEXYA cible 950 000 utilisateurs au Cameroun + Afrique francophone.
Chaque décision tient compte :
- **2G/3G** : timeouts longs, heartbeat SSE 15s, pagination cursor.
- **Mobile money** : CinetPay (Orange Money, MTN, Wave) + NotchPay
  (Airtel, MoovMoney) + Stripe carte (diaspora) — Phase 11.
- **Langues vernaculaires** : Duala, Bassa, Medumba, Fulfulde, Ewondo,
  Bamiléké via fine-tuning Gemma — Phase H.
- **OHADA** : droit des affaires harmonisé 17 pays africains —
  expert `legal` calibré.

---

## 💬 Support

- Email : support@nexya.ai
- Issues : GitHub Issues
- Incidents critiques : escalation auto Crisp pour users Pro (Phase 18 / N4)

---

**License** : Proprietary — Nexyalabs.
