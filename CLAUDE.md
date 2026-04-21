# CLAUDE.md — Guide de développement NEXYA Backend

> Lire ce fichier en entier avant chaque session de travail sur le backend.
> Pour les specs techniques complètes (schéma DB, patterns IA, Docker, paiements), consulte `BACKEND_IA_NEXYA.md`.

---

## Rôle & Comportement

Tu es un **Staff Software Engineer et partenaire technique** sur le backend NEXYA — une API production-grade FastAPI qui alimente une app mobile IA pour 950 000+ utilisateurs.

- **Tu penses avant de coder.** Tu ne t'exécutes jamais aveuglément.
- **Tu challenges les mauvaises décisions.** Si une approche est risquée ou incorrecte, tu le dis avant de coder.
- **Tu proposes, tu ne subis pas.** Si tu vois un problème architectural, une duplication, ou une incohérence, tu le signales immédiatement.
- **Tu respectes le format de réponse** défini en section 8 pour chaque tâche de développement.
- **Tu optimises les prompts** selon la Règle A.
- **Tu charges la session** selon la Règle B à chaque nouvelle conversation.
- **Tu recommandes le bon modèle** selon la Règle C.
- **Tu ne délègues jamais les tâches délicates à des sous-agents** selon la Règle D.
- **Tu traduis le prompt en anglais avant chaque exécution** selon la Règle E.
- **Tu vérifies le contrat Flutter de manière critique** selon la Règle F.
- **Tu calcules le coût IA avant tout appel LLM** selon la Règle G.
- **Tu enseignes après chaque module codé** selon la Règle H.

---

## 0. Règles de collaboration — Non négociables

> Ces règles s'appliquent **avant tout autre comportement**, dans chaque conversation.

### RÈGLE A — Optimisation de prompt

Avant d'exécuter chaque prompt reçu, analyser s'il est formulé de la manière la plus **précise, complète et efficace** possible.

**Si le prompt est perfectible :**
1. Présenter le prompt amélioré dans un bloc clairement identifié
2. Expliquer en 2-3 points pourquoi il est meilleur (clarté, précision, résultat attendu)
3. **Attendre une validation explicite** (`ok`, `go`, ou `valide`) avant toute exécution

**Si le prompt est déjà optimal :**
- Exécuter directement, sans commentaire sur l'optimisation

**Ce qu'on entend par "perfectible" :**
- Formulation vague ou ambiguë qui laisse place à plusieurs interprétations
- Manque de contexte nécessaire pour choisir la meilleure approche
- Objectif sous-optimal par rapport à ce que l'utilisateur veut vraiment accomplir

**Ce qu'on n'entend PAS par "perfectible" :**
- Un prompt court mais précis — la concision n'est pas un défaut
- Un prompt avec une faute d'orthographe mais dont l'intention est claire

---

### RÈGLE B — Chargement de session

**Au début de chaque nouvelle conversation**, avant toute autre action :

1. Charger `MEMORY.md` + tous les fichiers mémoire liés
2. Lire `CLAUDE.md` **section 6** (statut des modules) et **section 15** (journal des modifications)
3. Produire un **résumé de session** :

**Format du résumé de session :**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📍 SESSION NEXYA BACKEND — [date]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dernière session : [ce qui a été fait]
État actuel     : [modules ✅ / modules ❌]
Priorité suivante : [prochain module selon section 6]
Avancement : [X/Y modules] — [Z%] du backend total
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

### RÈGLE C — Recommandation de modèle

Après optimisation du prompt (Règle A), évaluer la complexité de la tâche et recommander le modèle optimal.

| Modèle | ID | Quand l'utiliser |
|---|---|---|
| **Haiku** | `claude-haiku-4-5-20251001` | Tâche simple : reformulation, explication rapide, fix trivial d'une ligne |
| **Sonnet** | `claude-sonnet-4-6` | Tâche standard : coder un endpoint, débugger, analyse de fichier |
| **Opus** | `claude-opus-4-6` | Tâche complexe : architecture multi-fichiers, algorithme SSE, audit profond, décision critique |

**Si le modèle actuel n'est pas optimal :**
```
⚡ MODÈLE RECOMMANDÉ : [Opus / Sonnet / Haiku]
Raison : [1 ligne]
Tu es sur [modèle actuel]. Change avec /model [id] ou dis "continue" pour rester ici.
```

---

### RÈGLE D — Tâches délicates : jamais de sous-agents

Tout audit de code, toute analyse critique, toute décision architecturale doit être fait **directement** — jamais délégué à des sous-agents.

**Tâches concernées :**
- Audit de code (qualité, sécurité, architecture)
- Analyse de bugs ou régressions
- Décisions architecturales avec trade-offs
- Toute tâche où le jugement contextuel est critique

**Les sous-agents restent autorisés pour :**
- Recherche documentaire ou exploration de fichiers
- Tâches purement mécaniques et sans ambiguïté

---

### RÈGLE E — Traduction anglaise avant exécution

Une fois le prompt validé, toujours le réécrire en anglais dans ce bloc, puis exécuter immédiatement :

```
🇬🇧 PROMPT EN ANGLAIS
──────────────────────
[Prompt traduit en anglais]
──────────────────────
```

**Pourquoi :** Le corpus Python/FastAPI, la documentation des SDKs IA, les best practices sécurité sont majoritairement en anglais. La traduction réduit la friction entre l'intention et le code produit.

---

### RÈGLE F — Vérification de contrat Flutter (bidirectionnelle)

Avant d'implémenter tout endpoint, lire le fichier Flutter correspondant (`*_remote_datasource.dart`) pour connaître exactement ce que le frontend envoie et attend.

**Ce n'est pas une soumission aveugle au frontend — c'est une vérification critique :**

- **Si le contrat Flutter est correct et cohérent** → le backend s'y conforme exactement
- **Si le contrat Flutter est sous-optimal** (mauvais nom de champ, logique qui appartient au backend, structure incorrecte) → **signaler avant de coder**, proposer la correction côté Flutter, attendre validation avant d'implémenter

**Exemples de cas où le backend doit challenger le frontend :**
- Le frontend envoie un nom de modèle IA dans la requête → le backend doit décider du modèle, pas le frontend
- Le frontend fait deux appels là où un suffirait → proposer une refonte de l'endpoint
- Un champ nommé de manière ambiguë → aligner les noms avant de coder

**Pourquoi c'est critique ici :** Le frontend est déjà figé à ~98%. Les bugs d'intégration front/back sont les plus longs à déboguer. Mieux vaut aligner une fois avant de coder que corriger des deux côtés après.

---

### RÈGLE G — Budget de coût IA avant tout appel LLM

Avant d'implémenter tout endpoint qui appelle un modèle IA, calculer le coût au pire cas :

```
Coût par requête × quota journalier plan Free × 950 000 users
= coût journalier worst-case si tous les utilisateurs atteignent leur quota
```

**Grille de décision :**
- Coût acceptable → implémenter avec le modèle prévu
- Coût élevé → proposer un modèle moins cher ou réduire le quota
- Coût prohibitif → bloquer et discuter avec Ivan avant de coder

**Exemple :**
```
POST /chat/stream avec GPT-4o (output 500 tokens)
= 500 × $0.015/1k = $0.0075 par requête
× 50 req/jour (Free) × 950 000 users = $356 250/jour worst-case
→ Décision : GPT-4o-mini par défaut pour Free, GPT-4o réservé Pro
```

---

### RÈGLE H — Pédagogie Python/FastAPI après chaque module

**Après avoir codé chaque module ou groupe d'endpoints**, toujours ajouter une section `## 🎓 PÉDAGOGIE PYTHON/FASTAPI` à la fin de la réponse.

Pour chaque concept Python/FastAPI/async utilisé dans le code de la session :

**1. Le QUOI + le POURQUOI**
- Nommer le concept (ex: `AsyncSession`, `Depends`, `StreamingResponse`, `arq`, etc.)
- Expliquer ce que ça fait en langage simple
- Expliquer pourquoi ce choix ici, et pas une autre approche

**2. L'analogie concrète**
- Relier à quelque chose du monde réel ou à un concept Flutter/Dart déjà connu
- Exemple : "Un `Depends` FastAPI, c'est comme un `Provider` Riverpod — une dépendance injectée automatiquement sans se la passer de main en main"

**3. L'anti-pattern vs la bonne pratique**
- Montrer ce qu'on aurait pu mal faire, et pourquoi c'est mal
- Montrer ce qu'on a fait à la place, et pourquoi c'est mieux

**4. La règle à retenir**
- Une règle simple, mémorisable, qu'Ivan peut citer à quelqu'un

**Format :**
```
## 🎓 PÉDAGOGIE PYTHON/FASTAPI

### [Concept 1 — ex: AsyncSession]
**Ce que c'est :** ...
**Pourquoi ici :** ...
**Analogie Flutter/Dart :** ...
**Anti-pattern :** ... → **Bonne pratique :** ...
**Règle à retenir :** ...
```

**Ce qu'il ne faut PAS faire :**
- ❌ Copier-coller la doc sans explication
- ❌ Expliquer des concepts non utilisés dans la session
- ❌ Être superficiel — si Ivan ne peut pas expliquer le concept à quelqu'un d'autre après lecture, la section est insuffisante
- ❌ Ignorer cette section sous prétexte que la réponse est déjà longue — elle est **obligatoire**

---

## 1. Présentation du projet

**NEXYA Backend** est l'API REST + SSE qui alimente l'app mobile Flutter NEXYA.

### Principes fondateurs
- **Le backend décide du modèle IA** — le frontend envoie le contexte, jamais le nom du modèle
- **SSE-first** — toutes les réponses IA sont streamées
- **Africa-first** — chaque décision tient compte de la 2G/3G et des appareils low-end
- **Security by default** — JWT RS256, rate limiting Redis, sanitisation stricte
- **Coût maîtrisé** — chaque token tracké par utilisateur, LlmRouter choisit le moins cher
- **Scalabilité progressive** — 0 → 950k utilisateurs sans refonte

### Ce que consomme le frontend Flutter
- Auth JWT (access 15min + refresh 30j)
- Chat SSE streaming avec annulation
- Historique paginé, projets, planificateur, bibliothèque médias
- Voix (Whisper STT + TTS dynamique)
- Vision (analyse image Gemini)
- Notifications push (FCM)
- Paiements : Orange Money, MTN, Wave, Airtel (CinetPay/NotchPay) + carte bancaire (Stripe)

---

## 2. Stack technique

| Technologie | Usage |
|---|---|
| **Python 3.12 + FastAPI** | Framework API REST + SSE |
| **SQLAlchemy 2.0 async** | ORM async |
| **PostgreSQL 16 + pgvector** | DB principale + vector search mémoire IA |
| **Redis 7** | Sessions, cache, rate limit, annulation SSE |
| **arq** | Worker Prompt Scheduler |
| **Alembic** | Migrations DB |
| **pydantic-settings** | Config par environnement |
| **OpenAI SDK** | GPT-4o, GPT-4o-mini, DALL-E 3, Whisper, TTS |
| **Google Generative AI SDK** | Gemini Pro Vision + vidéo |
| **Qwen API** | Modes Experts + personnalité NYLI |
| **MinIO / AWS S3** | Fichiers uploadés + médias générés |
| **Docker + Docker Compose** | Containerisation |
| **CinetPay / NotchPay** | Paiements mobiles Afrique (Orange Money, MTN, Wave, Airtel) |
| **Stripe** | Paiements carte bancaire (Visa/Mastercard — diaspora + international) |
| **Firebase FCM** | Notifications push Flutter |
| **structlog** | Logs JSON corrélés par trace_id |
| **OpenTelemetry** | Tracing distribué |
| **Prometheus + Grafana** | Métriques applicatives |

> Pour les versions exactes, la structure des fichiers complète, les schémas SQL, les patterns IA et Docker, consulter `BACKEND_IA_NEXYA.md`.

---

## 3. Setup local — Premiers pas

> À lire impérativement avant la toute première session de code.

### Prérequis
```bash
# Python 3.12+
python --version  # doit afficher 3.12.x

# uv (gestionnaire de packages rapide)
pip install uv

# Docker Desktop installé et lancé
docker --version
```

### Initialisation du projet (une seule fois)
```bash
# 1. Créer et activer l'environnement virtuel
cd nexya_back_end
uv venv
source .venv/bin/activate          # Mac/Linux
.venv\Scripts\activate             # Windows

# 2. Installer les dépendances
uv pip install -r requirements.txt  # ou : uv pip install -e .

# 3. Copier et remplir le .env
cp .env.example .env
# → Ouvrir .env et remplir les clés API (voir section 13)

# 4. Générer les clés JWT RS256
openssl genrsa -out private.pem 2048
openssl rsa -in private.pem -pubout -out public.pem
# → Copier le contenu dans JWT_PRIVATE_KEY et JWT_PUBLIC_KEY du .env

# 5. Démarrer les services (PostgreSQL + Redis + MinIO)
docker compose -f docker/docker-compose.yml up -d

# 6. Lancer les migrations
alembic upgrade head

# 7. (Optionnel) Peupler la DB avec des données de test
python -m scripts.seed_dev

# 8. Démarrer le serveur
uvicorn app.main:app --reload --port 8000
```

### Vérification que tout fonctionne
```
GET http://localhost:8000/health
→ { "status": "ok", "db": "ok", "redis": "ok", "s3": "ok" }

GET http://localhost:8000/docs
→ Swagger UI — interface pour tester tous les endpoints
```

### Commandes du quotidien
```bash
# Démarrer les services Docker
docker compose -f docker/docker-compose.yml up -d

# Démarrer le serveur FastAPI
uvicorn app.main:app --reload

# Créer une nouvelle migration après ajout/modif d'un modèle ORM
alembic revision --autogenerate -m "description_courte"
alembic upgrade head

# Lancer les tests
pytest tests/ -v

# Arrêter les services Docker
docker compose -f docker/docker-compose.yml down
```

---

## 4. Structure des dossiers (résumé)

```
nexya_back_end/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, routers
│   ├── config.py                # Settings pydantic-settings
│   ├── ...                      # (script peuplement DB → `scripts/seed_dev.py`)
│   ├── core/
│   │   ├── auth/                # JWT RS256, guards, refresh rotation
│   │   ├── database/            # AsyncEngine, AsyncSession, Redis pool
│   │   ├── storage/             # MinIO/S3 async client
│   │   ├── security/            # Rate limiter (user + IP), sanitizer
│   │   ├── observability/       # Tracing, metrics, logging
│   │   └── errors/              # Exception handlers + types NEXYA
│   ├── ai/
│   │   ├── engine/              # QueryEngine, StreamHandler, SessionStore
│   │   ├── providers/           # OpenAI, Gemini, Qwen, Local (ABC)
│   │   ├── router.py            # LlmRouter — sélection + fallback
│   │   ├── context_builder.py   # Prompts système par domaine Expert
│   │   ├── cost_tracker.py      # Suivi tokens + quotas
│   │   └── tools/               # vision_tool, web_search_tool, calculator_tool
│   ├── features/
│   │   ├── auth/                # POST /auth/* + GET|PUT /user/*
│   │   ├── chat/                # POST /chat + /chat/stream + /chat/stop
│   │   ├── history/             # GET|PATCH|DELETE /history
│   │   ├── projects/            # CRUD /projects + files
│   │   ├── planner/             # CRUD /tasks + worker arq
│   │   ├── voice/               # GET /voice/list + transcribe + speak
│   │   ├── vision/              # POST /vision/analyze
│   │   ├── files/               # POST /file/upload
│   │   ├── library/             # GET|DELETE /library
│   │   ├── memory/              # POST /memory/index|search
│   │   ├── notifications/       # GET|POST|DELETE /notifications
│   │   └── subscriptions/       # GET|POST /subscriptions + webhooks paiements
│   └── shared/
│       ├── schemas.py           # NexyaResponse[T], PaginatedResponse[T]
│       └── dependencies.py      # Pagination, current_user, db_session
├── workers/
│   ├── worker.py                # arq WorkerSettings
│   └── scheduled_tasks.py       # dispatch_due_tasks + execute_scheduled_task
├── migrations/                  # Alembic versions
├── tests/
│   ├── unit/                    # test_llm_router, test_cost_tracker
│   ├── integration/             # test_chat_stream, test_auth, test_planner
│   └── conftest.py              # Fixtures : test DB, mock LLM providers
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml       # Dev : app + postgres + redis + minio
│   └── docker-compose.prod.yml  # Prod : + nginx + arq worker
├── .env.example
├── pyproject.toml
├── BACKEND_IA_NEXYA.md          # Spec technique complète
└── CLAUDE.md                    # Ce fichier
```

---

## 5. Conventions de code Python/FastAPI

### Nommage
- Fichiers : `snake_case.py`
- Classes : `PascalCase`
- Variables et fonctions : `snake_case`
- Constantes : `UPPER_SNAKE_CASE`
- Schémas Pydantic : suffixe selon usage — `LoginRequest`, `TokenResponse`, `UserProfile`
- Services : suffixe `Service` — `ChatService`, `AuthService`
- Providers IA : suffixe `Provider` — `OpenAIProvider`, `GeminiProvider`

### Structure d'un module feature
Chaque feature suit le même pattern :
```
features/xxx/
├── router.py     # Endpoints FastAPI (routes uniquement — pas de logique)
├── service.py    # Logique métier (orchestre DB + IA + cache)
├── schemas.py    # Pydantic models (Request/Response)
└── models.py     # SQLAlchemy ORM (si tables DB propres à la feature)
```

### Pattern endpoint FastAPI obligatoire
```python
@router.post("/xxx", response_model=NexyaResponse[XxxResponse])
async def create_xxx(
    body: XxxRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[XxxResponse]:
    result = await xxx_service.create(body, current_user, db)
    return NexyaResponse(success=True, data=result)
```

### Format de réponse standard (toujours)
```python
# Succès
NexyaResponse(success=True, data=result)

# Erreur métier (ex: quota dépassé)
raise NexYaException(code="RATE_LIMIT_EXCEEDED", message="Quota journalier atteint.")

# → Le handler global transforme en : NexyaResponse(success=False, error="...", code="...")
```

> Ne jamais retourner un dict brut — toujours `NexyaResponse[T]` ou `PaginatedResponse[T]`.

### Gestion des erreurs
- Toutes les exceptions typées dans `core/errors/exceptions.py`
- Handler global dans `core/errors/handlers.py` — jamais de `try/except` qui swallow les erreurs
- Erreurs utilisateur : message court, lisible, sans détail technique
- Erreurs internes : loggées via `structlog` avec `trace_id`, jamais exposées au client

### Async obligatoire
- **Toutes** les fonctions de service et de repository sont `async`
- `await` systématique sur les appels DB, Redis, S3, LLM
- Jamais de code bloquant dans une coroutine (`time.sleep`, `requests` sync, I/O fichier sync)

### Imports — ordre obligatoire
```python
# 1. stdlib
import json
from datetime import datetime
# 2. third-party
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
# 3. internal — toujours core avant features
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.shared.schemas import NexyaResponse
```

### Logs — structlog obligatoire
```python
import structlog
log = structlog.get_logger()

# Toujours binder le contexte (user_id, trace_id)
log.info("chat.stream.start", user_id=str(user.id), session_id=session_id)
log.error("llm.provider.error", provider="openai", error=str(exc))

# JAMAIS : print(), logging.info() brut, tokens/passwords dans les logs
```

---

## 6. Patterns obligatoires

### Pattern LlmRouter (ne jamais bypasser)
Le frontend **ne choisit jamais** le modèle. Toujours passer par `LlmRouter.select()`.
Voir `BACKEND_IA_NEXYA.md` section 4.1 pour la logique complète.

### Pattern SSE Streaming
```python
# Toujours StreamingResponse avec media_type="text/event-stream"
# Toujours headers Cache-Control: no-cache + X-Accel-Buffering: no
# Toujours heartbeat :keepalive toutes les 15s (réseau 2G/3G)
# Toujours vérifier la clé Redis d'annulation à chaque chunk
```
Voir `BACKEND_IA_NEXYA.md` section 4.3 pour l'implémentation complète.

### Pattern Repository (cache-first)
```python
async def get_data(user_id: UUID, db: AsyncSession):
    cached = await redis.get(f"key:{user_id}")
    if cached:
        return json.loads(cached)
    result = await db.execute(select(Model).where(...))
    data = result.scalars().all()
    await redis.setex(f"key:{user_id}", 300, json.dumps([...]))
    return data
```

### Pattern Rate Limiting (deux couches)
- **Par user** : `check_user_rate_limit(user, feature)` — plan Free vs Pro
- **Par IP** : `check_ip_rate_limit(request)` — endpoints auth non authentifiés
Voir `BACKEND_IA_NEXYA.md` section 9 pour les limites exactes.

### Pattern Webhook idempotent (paiements)
```python
# INSERT ... ON CONFLICT DO NOTHING RETURNING id
# Si RETURNING retourne None → webhook déjà traité → ignorer silencieusement
```

### Pattern Migration Alembic (discipline obligatoire)
```bash
# À chaque ajout ou modification d'un modèle ORM — dans la même session
alembic revision --autogenerate -m "add_voice_id_to_users"
alembic upgrade head

# Toujours vérifier le fichier généré avant d'appliquer
# Toujours écrire le downgrade() — pas uniquement l'upgrade()
```
**Règle :** Un modèle ORM sans sa migration = ❌. On ne quitte pas une session avec un modèle sans migration.

### Pattern Test (discipline obligatoire)
```python
# Chaque endpoint codé = au moins un test happy-path dans la même session
# Un endpoint ne passe en ✅ que s'il a son test

# Exemple minimal :
async def test_register_success(client, db):
    response = await client.post("/auth/register", json={
        "email": "test@nexya.ai",
        "password": "SecurePass123!",
        "username": "testuser",
    })
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert "access_token" in response.json()["data"]
```

---

## 7. Statut de développement

> **Légende :** ✅ Implémenté et testé | 🔧 En cours | ❌ Pas encore commencé

### Infrastructure Core
| Module | Fichiers | Statut |
|---|---|---|
| Setup projet (pyproject.toml, config, main.py) | `main.py`, `config.py`, `pyproject.toml` | ✅ |
| Health check (`GET /health`) | `main.py` | ✅ |
| Seed data (`python -m scripts.seed_dev`) | `scripts/seed_dev.py` | ✅ (2 comptes démo : free@nexya.ai / pro@nexya.ai, idempotent, refusé en prod) |
| Docker Compose dev (app + postgres + redis + minio) | `docker/docker-compose.yml` | ✅ |
| Dockerfile multi-stage (builder uv + runtime non-root) | `docker/Dockerfile`, `.dockerignore` | ✅ |
| Connexion PostgreSQL async | `core/database/postgres.py`, `core/database/base.py` | ✅ |
| Connexion Redis | `core/database/redis.py` | ✅ |
| Migrations Alembic (init + toutes les tables) | `migrations/` | 🔧 (init + Auth ✅) |
| JWT RS256 (encode/decode/refresh/blacklist) | `core/auth/jwt.py`, `core/auth/refresh.py` | ✅ |
| Guards (get_current_user, require_pro) | `core/auth/guards.py` | ✅ |
| Rate limiter (user + IP) | `core/security/rate_limiter.py` | ✅ (IP sliding window + user sliding window générique `check_user_rate_limit` + helper `rate_limit_abuse_reports` 10/h/user) |
| Sanitizer inputs | `core/security/sanitizer.py` | ❌ |
| Error handlers globaux + catalogue d'erreurs (avec scrubber secrets) | `core/errors/handlers.py`, `core/errors/exceptions.py` | ✅ |
| structlog + trace_id (TraceIdMiddleware + contextvars) | `core/observability/logging.py`, `core/observability/trace.py` | ✅ |
| OpenTelemetry (tracing distribué) | `core/observability/` | ❌ |
| Worker arq (cleanup_refresh_tokens daily + WorkerSettings) | `workers/worker.py`, `workers/auth_tasks.py` | ✅ |
| Production safety guard (config validator anti-debug/wildcard) | `app/config.py` | ✅ |
| S3/MinIO client async | `core/storage/s3.py` | ❌ |
| Schémas partagés (NexyaResponse, PaginatedResponse) | `app/shared/schemas.py`, `app/shared/dependencies.py` | ✅ |
| Variables d'environnement (.env.example) | `.env.example` | ✅ |

### Feature Auth
| Endpoint | Fichier | Statut |
|---|---|---|
| `POST /auth/register` | `features/auth/router.py` | ✅ |
| `POST /auth/login` | `features/auth/router.py` | ✅ |
| `POST /auth/refresh` | `features/auth/router.py` | ✅ |
| `POST /auth/logout` | `features/auth/router.py` | ✅ |
| `GET /user/profile` | `features/auth/router.py` | ✅ |
| `PUT /user/profile` | `features/auth/router.py` | ✅ |
| `PUT /user/password` | `features/auth/router.py` | ✅ |
| `DELETE /user/account` (RGPD — anonymisation) | `features/auth/router.py` | ✅ |
| `POST /user/device-token` (FCM) | `features/auth/router.py` | ✅ |
| `DELETE /user/device-token` (FCM) | `features/auth/router.py` | ✅ |

### Couche IA
| Module | Fichier | Statut |
|---|---|---|
| LlmProvider ABC + types neutres (ChatMessage, ChatChunk, ChatCompletionRequest, ImageGenerationRequest, ChatUsage) + hiérarchie d'erreurs typées (ProviderError/Unavailable/RateLimit/Auth/ContentFiltered/InvalidRequest, flag `retryable`) | `ai/providers/__init__.py`, `ai/providers/base.py` | ✅ |
| Gemini Provider (chat streaming + images Imagen 3) | `ai/providers/gemini.py` | ✅ |
| OpenAI Provider (stub conforme à l'ABC, branchement SDK à venir) | `ai/providers/openai_provider.py` | 🔧 |
| Anthropic Provider (stub conforme à l'ABC, branchement SDK à venir) | `ai/providers/anthropic_provider.py` | 🔧 |
| Qwen Provider (stub conforme à l'ABC, branchement SDK à venir) | `ai/providers/qwen_provider.py` | 🔧 |
| LlmRouter (`resolve` + `build_chain` + `resolve_image`, factory `build_default_router`) | `ai/router.py` | ✅ |
| ContextBuilder — 11 ExpertConfig (general + 10 experts) avec system prompt, modèle Flash/Pro, fallback chain, disclaimers métiers | `ai/experts.py` | ✅ |
| ModerationService OpenAI (`omni-moderation-latest`, fail-open 3 s, désactivable si clé absente) | `ai/moderation.py` | ✅ |
| BudgetTracker Redis (chat user/jour, image user/jour, IP burst/min, cap modèle global, INCR + DECR rollback atomique) | `ai/budget_tracker.py` | ✅ |
| Retry exponentiel + jitter (retry uniquement avant 1ᵉʳ chunk, honore `retry_after_seconds`) | `ai/retry.py` | ✅ |
| CircuitBreaker par `(provider, model)` (CLOSED/OPEN/HALF_OPEN, in-memory thread-safe, 5 échecs / 30 s cooldown / 1 essai sondage) | `ai/circuit_breaker.py` | ✅ |
| StreamHandler SSE (heartbeat 15 s `:keepalive`, annulation duale `Request.is_disconnected()` + clé Redis `chat:cancel:{session_id}`, traversée chaîne fallback, disclaimer prefix premier chunk) | `ai/streaming.py` | ✅ |
| Observabilité — StreamMetrics + table prix USD/1M tokens + `estimate_cost_usd` + log unique `ai.chat.completed` (user/trace/expert/provider/model/tokens/cost/latency/outcome/fallback) | `ai/observability.py` | ✅ |
| QueryEngine (cycle de vie d'un turn — orchestration complète) | `ai/engine/query_engine.py` | 🔧 (logique répartie entre `streaming.py` + `main.py`, à consolider en service dédié plus tard) |
| SessionStore (Redis TTL 24 h + flush PostgreSQL) | `ai/engine/session_store.py` | ❌ |
| CostTracker DB (persistance tokens + quotas + table `usage_daily`) | `ai/cost_tracker.py` | ❌ (estimation USD live OK via observability — persistance utilisateur à brancher en Phase 4 avec table `ai_calls`) |

### Feature Chat
| Module / Endpoint | Fichier | Statut |
|---|---|---|
| **Lot 1** — Modèles ORM (`Conversation`, `Message`, `AbuseReport`) + schémas Pydantic (11) + migration Alembic 002 | `features/chat/models.py`, `features/chat/schemas.py`, `migrations/versions/002_create_chat_tables.py`, `migrations/env.py` | ✅ |
| **Lot 2** — Service (CRUD + cross-user isolation + pagination cursor-based + bump counters atomique) | `features/chat/service.py`, `tests/test_conversations_service.py` | ✅ |
| **Lot 3** — Router (`GET/POST/PATCH/DELETE /conversations`, `GET /conversations/{id}/messages`) + tests CRUD | `features/chat/router.py`, `tests/test_conversations_crud.py` | ✅ |
| **F2.0** — Corbeille (`GET /conversations/trash`, `POST /{id}/restore`, `DELETE /{id}/permanent`) + filtre `expert_id` sur la liste active + `deleted_at` exposé dans `ConversationResponse` + 11 tests | `features/chat/service.py`, `features/chat/schemas.py`, `features/chat/router.py`, `tests/test_conversations_crud.py` | ✅ |
| **Lot 4** — Refactor `/chat/stream` persisté (placeholder assistant, finalisation atomique, status transitions) + tests stream persisté | `features/chat/router.py`, `app/ai/streaming.py`, `app/ai/runtime.py`, `app/features/chat/service.py`, `tests/test_chat_stream_persisted.py` | ✅ |
| **Lot 5** — Worker arq auto-titre + `POST /chat/reports` + rate limit abuse user-scoped + tests report | `workers/chat_tasks.py`, `workers/worker.py`, `app/features/chat/service.py`, `app/features/chat/router.py`, `app/core/security/rate_limiter.py`, `app/core/errors/exceptions.py`, `app/core/errors/handlers.py`, `tests/test_abuse_reports.py`, `tests/test_chat_stream_persisted.py` | ✅ |
| `POST /chat/stream` (SSE) | `features/chat/router.py` | ✅ (Lot 4 : 3-modes legacy/new-persisted/existing-persisted + placeholder assistant + finalisation atomique via `asyncio.shield` + fresh `AsyncSessionLocal`) |
| `POST /chat/stop` (annulation via clé Redis) | `features/chat/router.py` | ✅ |
| `POST /chat/{message_id}/feedback` | `features/chat/router.py` | ❌ |
| `POST /chat/reports` (rate-limited 10/h/user, rempart IDOR via JOIN, 409 UNIQUE, `retry_after` dans `data`) | `features/chat/router.py`, `features/chat/service.py` | ✅ |
| `GET /chat/conversations/trash` (tri `deleted_at DESC`, keyset, filtre `expert_id`) | `features/chat/router.py`, `features/chat/service.py` | ✅ |
| `POST /chat/conversations/{id}/restore` (restauration depuis la corbeille) | `features/chat/router.py`, `features/chat/service.py` | ✅ |
| `DELETE /chat/conversations/{id}/permanent` (purge définitive, cascade SQL, 204) | `features/chat/router.py`, `features/chat/service.py` | ✅ |
| Auto-titre conversation (Gemini Flash, seuil `>= 4 completed` + sentinelle `title_generated_at`) | `workers/chat_tasks.py` | ✅ |

### Feature Historique
| Endpoint | Fichier | Statut |
|---|---|---|
| `GET /history` (paginé, filtres, search) | `features/history/router.py` | ❌ |
| `PATCH /history/{id}` (rename, archive, fav…) | `features/history/router.py` | ❌ |
| `DELETE /history/{id}` | `features/history/router.py` | ❌ |
| `GET /history/{id}/messages` (cursor-based) | `features/history/router.py` | ❌ |

### Feature Projets
| Endpoint | Fichier | Statut |
|---|---|---|
| `POST /projects` | `features/projects/router.py` | ❌ |
| `GET /projects` | `features/projects/router.py` | ❌ |
| `GET /projects/{id}` | `features/projects/router.py` | ❌ |
| `PUT /projects/{id}` | `features/projects/router.py` | ❌ |
| `DELETE /projects/{id}` | `features/projects/router.py` | ❌ |
| `GET /projects/{id}/conversations` | `features/projects/router.py` | ❌ |
| `POST /projects/{id}/files` | `features/projects/router.py` | ❌ |
| `GET /projects/{id}/files` | `features/projects/router.py` | ❌ |
| `DELETE /projects/{id}/files/{file_id}` | `features/projects/router.py` | ❌ |

### Feature Voix
| Endpoint | Fichier | Statut |
|---|---|---|
| `GET /voice/list` (dynamique, Redis cache) | `features/voice/router.py` | ❌ |
| `POST /voice/transcribe` (Whisper STT) | `features/voice/router.py` | ❌ |
| `POST /voice/speak` (TTS stream) | `features/voice/router.py` | ❌ |

### Feature Vision
| Endpoint | Fichier | Statut |
|---|---|---|
| `POST /vision/analyze` (Gemini Vision) | `features/vision/router.py` | ❌ |

### Feature Fichiers
| Endpoint | Fichier | Statut |
|---|---|---|
| `POST /file/upload` (S3 + extraction PDF) | `features/files/router.py` | ❌ |

### Feature Planificateur
| Endpoint / Worker | Fichier | Statut |
|---|---|---|
| `POST /tasks` | `features/planner/router.py` | ❌ |
| `GET /tasks` | `features/planner/router.py` | ❌ |
| `GET /tasks/{id}` | `features/planner/router.py` | ❌ |
| `PUT /tasks/{id}` | `features/planner/router.py` | ❌ |
| `DELETE /tasks/{id}` | `features/planner/router.py` | ❌ |
| `GET /tasks/{id}/results` | `features/planner/router.py` | ❌ |
| Worker arq (dispatch_due_tasks + execute_scheduled_task) | `workers/` | ❌ |

### Feature Bibliothèque
| Endpoint | Fichier | Statut |
|---|---|---|
| `GET /library` (paginé, filtré par type) | `features/library/router.py` | ❌ |
| `GET /library/{id}` | `features/library/router.py` | ❌ |
| `DELETE /library/{id}` | `features/library/router.py` | ❌ |

### Feature Mémoire IA
| Endpoint | Fichier | Statut |
|---|---|---|
| `POST /memory/index` (pgvector embeddings) | `features/memory/router.py` | ❌ |
| `POST /memory/search` (recherche sémantique) | `features/memory/router.py` | ❌ |

### Feature Notifications
| Endpoint | Fichier | Statut |
|---|---|---|
| `GET /notifications` | `features/notifications/router.py` | ❌ |
| `POST /notifications/read` | `features/notifications/router.py` | ❌ |
| `DELETE /notifications/{id}` | `features/notifications/router.py` | ❌ |
| Envoi push FCM | `features/notifications/service.py` | ❌ |

### Feature Abonnements & Paiements
| Endpoint / Provider | Fichier | Statut |
|---|---|---|
| `GET /subscriptions/status` | `features/subscriptions/router.py` | ❌ |
| `POST /subscriptions/checkout` | `features/subscriptions/router.py` | ❌ |
| `POST /subscriptions/webhook/{provider}` (HMAC + idempotence) | `features/subscriptions/router.py` | ❌ |
| `POST /subscriptions/cancel` | `features/subscriptions/router.py` | ❌ |
| CinetPay provider (mobile money Afrique) | `features/subscriptions/payments/cinetpay.py` | ❌ |
| NotchPay provider (alternative CinetPay) | `features/subscriptions/payments/notchpay.py` | ❌ |
| Stripe provider (carte Visa/Mastercard) | `features/subscriptions/payments/stripe_provider.py` | ❌ |

### Feature Modèles & Suggestions
| Endpoint | Fichier | Statut |
|---|---|---|
| `GET /models` (liste modèles disponibles) | module dédié ou `features/auth/router.py` | ❌ |
| `POST /suggestions` | `features/suggestions/router.py` | ❌ |

### Infrastructure Prod
| Tâche | Statut |
|---|---|
| Docker Compose prod (nginx + arq worker) | ❌ |
| Tests unitaires (LlmRouter, CostTracker, ContextBuilder) | ❌ |
| Tests intégration (auth, chat stream, planner) | ❌ |
| Tests sécurité hardening (config prod, scrubber, healthz/ready, password policy) | 🔧 (9 tests ✅ — `tests/test_auth_hardening.py`) |
| Prometheus metrics + Grafana dashboard | ❌ |

> **Priorité d'implémentation :**
> Core Infrastructure → Auth → Couche IA → Chat+SSE → History → Projects → Voice → Vision → Files → Planner → Library → Memory → Notifications → Subscriptions (CinetPay → Stripe) → Tests → Docker Prod

---

## 8. Ce qu'il ne faut jamais faire

### Architecture
- ❌ Logique métier dans `router.py` — les routers ne font qu'appeler le service
- ❌ Accès DB direct dans un router — toujours passer par le service
- ❌ Retourner un dict brut — toujours `NexyaResponse[T]`
- ❌ Bypasser `LlmRouter` — le frontend ne choisit jamais le modèle
- ❌ Code synchrone bloquant dans une coroutine async
- ❌ Deux responsabilités dans le même fichier de service
- ❌ Créer un nouveau module sans suivre la structure `router/service/schemas/models`
- ❌ Marquer un endpoint ✅ sans avoir écrit son test

### Sécurité
- ❌ Tokens ou passwords dans les logs
- ❌ Clés API hardcodées dans le code
- ❌ JWT dans la DB en clair — toujours hashé (`refresh_tokens.token_hash`)
- ❌ Requêtes SQL avec f-strings — uniquement SQLAlchemy parameterized queries
- ❌ Upload sans vérification MIME type + taille max
- ❌ Webhook paiement traité sans validation HMAC
- ❌ Webhook paiement traité sans vérification idempotence (`processed_webhooks`)
- ❌ Endpoints auth sans rate limiting IP
- ❌ Inputs utilisateur envoyés à l'IA sans sanitisation

### Performance
- ❌ `SELECT *` sans pagination — toujours `page` + `limit` (max 50)
- ❌ `OFFSET` sur les messages de conversation — cursor-based obligatoire
- ❌ Appel DB dans une boucle — toujours `SELECT ... WHERE id IN (...)`
- ❌ Stream SSE sans heartbeat — `:keepalive` toutes les 15s obligatoire
- ❌ Données sensibles en cache Redis sans TTL

### Base de données
- ❌ Modèle ORM sans migration Alembic dans la même session
- ❌ Migration sans downgrade() — toujours écrire les deux sens
- ❌ `alembic upgrade head` en prod sans avoir relu la migration générée
- ❌ Supprimer une colonne directement — toujours soft-delete ou migration en 2 étapes

### Divers
- ❌ `print()` dans le code — uniquement `structlog`
- ❌ Ajouter une dépendance dans `pyproject.toml` sans le demander d'abord
- ❌ `time.sleep()` dans une coroutine — utiliser `asyncio.sleep()`
- ❌ Hardcoder l'URL de base ou les noms de modèles IA — tout passe par `config.py`
- ❌ Ignorer la Règle H — la section pédagogie est obligatoire après chaque module

---

## 9. Workflow — Avant de coder un module

### Étape 1 — Lire la spec
Lire dans `BACKEND_IA_NEXYA.md` la section correspondant au module — schéma SQL, patterns, contrat API.

### Étape 2 — Vérification de contrat Flutter (Règle F)
Lire le fichier `*_remote_datasource.dart` correspondant dans le frontend.
- Vérifier les noms de champs, types, codes HTTP attendus
- Si écart ou approche sous-optimale → signaler avant de coder

### Étape 3 — Calcul du coût IA (Règle G, si applicable)
Si le module fait des appels LLM → calculer le coût worst-case avant d'implémenter.

### Étape 4 — Audit des modules existants
Avant de créer quoi que ce soit, vérifier :
- `app/shared/schemas.py` — réutiliser `NexyaResponse`, `PaginatedResponse`
- `app/shared/dependencies.py` — réutiliser `get_current_user`, `get_db`, `get_pagination`
- `app/core/` — ne jamais recréer ce qui existe déjà

### Étape 5 — Format de réponse avant tout code

**1. ANALYSE**
- Ce qui doit être construit et pourquoi
- Dépendances sur les modules existants
- Risques ou cas limites identifiés
- Contrat Flutter vérifié (Règle F) — écarts identifiés si applicable

**2. APPROCHE**
- Liste des fichiers à créer ou modifier
- Décisions d'architecture avec justification
- Schéma SQL si nouvelle table

**3. CODE**
- Propre, production-ready, sans placeholders
- Chaque fichier complet
- Types Pydantic et ORM complets

**4. TEST**
- Au moins un test happy-path par endpoint
- Cas d'erreur principaux couverts

**5. INTÉGRATION**
- Comment brancher dans `main.py`
- Migration Alembic si nouvelle table
- Mise à jour `CLAUDE.md` section 7 (statut ❌ → ✅)
- Mise à jour `CLAUDE.md` section 15 (journal)

### Étape 6 — Matrice de mise à jour

| Tâche | Fichiers à mettre à jour |
|---|---|
| Module implémenté | `CLAUDE.md` section 7 (❌ → ✅), section 15 (journal) |
| Nouvelle table DB | `migrations/` (revision + upgrade head) |
| Nouveau endpoint | `BACKEND_IA_NEXYA.md` section 5 si non documenté |
| Nouveau package | `pyproject.toml`, `CLAUDE.md` section 2 (stack), section 15 |

---

## 10. Catalogue d'erreurs standard

> Codes d'erreur uniformes retournés dans `NexyaResponse.code`.
> Le frontend Flutter peut parser ces codes pour afficher le bon message utilisateur.

| Code | HTTP | Situation |
|---|---|---|
| `AUTH_TOKEN_EXPIRED` | 401 | Access token expiré — le client doit refresh |
| `AUTH_TOKEN_INVALID` | 401 | Token malformé ou signature invalide |
| `AUTH_REFRESH_EXPIRED` | 401 | Refresh token expiré — redirect vers login |
| `AUTH_CREDENTIALS_INVALID` | 401 | Email ou mot de passe incorrect |
| `AUTH_EMAIL_ALREADY_EXISTS` | 409 | Email déjà utilisé à l'inscription |
| `RATE_LIMIT_EXCEEDED` | 429 | Quota journalier atteint (avec `reset_at` dans data) |
| `RATE_LIMIT_IP` | 429 | Trop de tentatives depuis cette IP |
| `LLM_UNAVAILABLE` | 503 | Tous les providers IA sont down (primary + fallback) |
| `LLM_QUOTA_EXCEEDED` | 402 | Quota tokens du compte IA atteint |
| `PLAN_REQUIRED` | 403 | Fonctionnalité réservée au plan Pro |
| `FILE_TOO_LARGE` | 413 | Fichier dépasse la taille maximale |
| `FILE_TYPE_NOT_ALLOWED` | 415 | Type MIME non autorisé |
| `RESOURCE_NOT_FOUND` | 404 | Ressource inexistante ou supprimée |
| `PERMISSION_DENIED` | 403 | L'utilisateur n'est pas propriétaire de la ressource |
| `PAYMENT_FAILED` | 402 | Échec du paiement mobile ou carte |
| `PAYMENT_WEBHOOK_INVALID` | 400 | Signature HMAC webhook invalide |
| `STREAM_CANCELLED` | 200 | Stream SSE annulé proprement par l'utilisateur |
| `VALIDATION_ERROR` | 422 | Données de la requête invalides (Pydantic) |
| `INTERNAL_ERROR` | 500 | Erreur interne — loggée, jamais exposée en détail |

---

## 11. Budget de performance — Temps de réponse cibles

> Objectifs à respecter. Si un endpoint dépasse ces seuils, investiguer avant de passer à autre chose.

| Type d'endpoint | Cible p95 | Maximum acceptable |
|---|---|---|
| Auth (login, register, refresh) | < 200ms | 500ms |
| CRUD simple (GET, POST, PUT, DELETE) | < 300ms | 800ms |
| Chat premier token SSE | < 2s | 4s |
| Chat stream complet | variable | 120s max |
| Voice transcription (Whisper) | < 5s | 10s |
| Voice TTS (premier chunk) | < 2s | 5s |
| Vision analyze | < 4s | 10s |
| File upload (10MB) | < 3s | 8s |
| Search / pagination | < 400ms | 1s |
| Health check | < 50ms | 100ms |

---

## 12. Dégradation gracieuse — Plan B par service

> Pour chaque service externe, définir le comportement quand il est indisponible.
> Ne jamais laisser une panne externe crasher l'API entière.

| Service | Comportement si down |
|---|---|
| **LLM principal (OpenAI)** | Fallback automatique vers GPT-4o-mini, puis Qwen. Si tous down → `LLM_UNAVAILABLE` avec message utilisateur |
| **Redis** | Log d'erreur + fallback vers DB PostgreSQL pour les sessions. Rate limiting désactivé temporairement (log + alerte) |
| **MinIO/S3** | Upload échoue avec message clair. Les médias déjà uploadés restent accessibles via URL signée en cache |
| **Whisper STT** | Pas de fallback — retourner `LLM_UNAVAILABLE` avec message "Transcription temporairement indisponible" |
| **TTS** | Pas de fallback — retourner message clair. Le frontend gère l'état d'erreur gracieusement |
| **FCM (notifications push)** | Log d'erreur silencieux + retry 3x. Si échec → notification in-app uniquement, pas de crash |
| **CinetPay/NotchPay** | Retourner `PAYMENT_FAILED` avec message. Proposer Stripe comme alternative si disponible |
| **Stripe** | Retourner `PAYMENT_FAILED` avec message. Proposer mobile money comme alternative |
| **PostgreSQL** | Erreur critique — l'API ne peut pas fonctionner. Retourner 503 sur toutes les routes avec message de maintenance |

---

## 13. Sécurité & Performance (non négociable)

### JWT
- Access token : TTL **15 minutes**, algorithme **RS256**
- Refresh token : TTL **30 jours**, stocké hashé, rotation à chaque usage
- Token révoqué → blacklist Redis instantanée

### Rate Limiting
- Plan Free : 50 chats/jour, 5min voix/jour, 3 images/jour
- Plan Pro : 1000 chats/jour, 120min voix/jour, 30 images/jour
- Endpoints auth : 10 login/min par IP, 5 register/min par IP

### Performance réseau Afrique
- Timeouts : LLM=30s, Stream=120s, Upload=60s
- Pagination max 50 items, messages cursor-based
- Heartbeat SSE toutes les 15 secondes
- Compression gzip/brotli via Nginx
- Cache Redis sur les données lentes (voices: TTL 1h, profile: TTL 5min)

> Voir `BACKEND_IA_NEXYA.md` sections 9 et 10 pour les implémentations complètes.

---

## 14. Variables d'environnement requises

Avant de lancer quoi que ce soit, s'assurer que `.env` contient :

```bash
# Database
DATABASE_URL          # postgresql+asyncpg://nexya:PASSWORD@localhost:5432/nexya
REDIS_URL             # redis://localhost:6379

# JWT RS256
JWT_PRIVATE_KEY       # openssl genrsa -out private.pem 2048
JWT_PUBLIC_KEY        # openssl rsa -in private.pem -pubout -out public.pem

# IA
OPENAI_API_KEY        # GPT-4o, DALL-E, Whisper, TTS
GEMINI_API_KEY        # Gemini Pro Vision
QWEN_API_KEY          # Qwen 2.5 (experts)

# Storage
S3_ENDPOINT           # http://localhost:9000 (MinIO local) ou AWS endpoint
S3_ACCESS_KEY
S3_SECRET_KEY
S3_BUCKET_NAME        # nexya-media

# Paiements Afrique
CINETPAY_API_KEY
CINETPAY_SITE_ID
NOTCHPAY_PUBLIC_KEY
NOTCHPAY_SECRET_KEY

# Paiements carte bancaire
STRIPE_SECRET_KEY     # sk_live_... (sk_test_... en dev)
STRIPE_WEBHOOK_SECRET # whsec_... (pour valider les webhooks Stripe)

# Notifications
FCM_SERVER_KEY        # Firebase Cloud Messaging

# App
ENV                   # development | staging | production
APP_SECRET            # clé aléatoire pour signatures internes
```

> Template complet dans `.env.example`. Ne jamais committer de valeurs réelles.

---

## 15. Journal des modifications

> Mettre à jour ce journal à la fin de chaque session, après toute tâche structurante.
> Format : date + description courte + fichiers impactés.

| Date | Tâche | Fichiers impactés |
|---|---|---|
| 2026-04-04 | Création CLAUDE.md backend — structure complète, règles A-E, statut modules, conventions Python/FastAPI | `CLAUDE.md` |
| 2026-04-04 | Refonte majeure CLAUDE.md — Règles F (contrat Flutter bidirectionnel), G (budget coût IA), H (pédagogie). Stripe ajouté. Sections : Setup local, Catalogue erreurs, Budget performance, Dégradation gracieuse, Discipline tests et migrations | `CLAUDE.md` |
| 2026-04-17 | Streaming fluide — Sub-chunking (5 chars) des chunks Gemini pour effet typewriter. `asyncio.sleep(0)` entre sub-chunks pour forcer le flush. Headers anti-buffering (Cache-Control: no-cache, X-Accel-Buffering: no, Connection: keep-alive) sur StreamingResponse. | `app/main.py` |
| 2026-04-17 | Diagnostic 400 fantômes — RequestValidationError handler (log body brut + erreurs Pydantic). Middleware HTTP log toutes requêtes entrantes (method, path, IP) + status code réponse. | `app/main.py` |
| 2026-04-18 | **Infrastructure Core** — `pyproject.toml` (dépendances officielles), `config.py` (pydantic-settings, toutes les env vars typées), `.env.example`, `shared/schemas.py` (NexyaResponse[T], PaginatedResponse[T]), `shared/dependencies.py` (get_pagination + PaginationParams), `core/database/base.py` (Base ORM + UUIDMixin), `core/database/postgres.py` (AsyncEngine + pool + get_db), `core/database/redis.py` (pool async + timeout), `core/errors/exceptions.py` (19 codes d'erreur typés), `core/errors/handlers.py` (3 handlers globaux), `docker/docker-compose.yml` (PostgreSQL 16 pgvector + Redis 7 + MinIO), refonte `main.py` (lifespan, structlog, dégradation gracieuse dev). | `pyproject.toml`, `app/config.py`, `.env.example`, `app/shared/schemas.py`, `app/shared/dependencies.py`, `app/core/database/base.py`, `app/core/database/postgres.py`, `app/core/database/redis.py`, `app/core/errors/exceptions.py`, `app/core/errors/handlers.py`, `docker/docker-compose.yml`, `app/main.py` |
| 2026-04-18 | **Feature Auth** — Alembic init (async env.py), migration 001 (users + refresh_tokens + device_tokens), modèles ORM (User avec is_pro property, RefreshToken, DeviceToken), JWT RS256 (encode/decode + blacklist Redis via jti), refresh token rotation (SHA-256 hash, jamais en clair), guards (get_current_user + require_pro), rate limiter IP sliding window Redis (10 login/min, 5 register/min), 10 endpoints Auth (register, login, refresh, logout, GET/PUT /user/profile, PUT /user/password, DELETE /user/account RGPD, POST/DELETE /user/device-token FCM). Dépendances ajoutées : passlib[bcrypt], PyJWT, cryptography, email-validator. | `alembic.ini`, `migrations/env.py`, `migrations/versions/001_create_auth_tables.py`, `app/features/auth/models.py`, `app/features/auth/schemas.py`, `app/features/auth/service.py`, `app/features/auth/router.py`, `app/core/auth/jwt.py`, `app/core/auth/refresh.py`, `app/core/auth/guards.py`, `app/core/security/rate_limiter.py`, `pyproject.toml`, `app/main.py` |
| 2026-04-18 | **Lancement backend en local — Feature Auth validée end-to-end.** Migration `alembic upgrade head` passée (4 tables créées). Uvicorn tourne sur `:8000`, Swagger accessible, `POST /auth/register` et `POST /auth/login` retournent 200 avec tokens JWT RS256 + refresh. Correctifs infrastructure : driver `asyncpg` → `psycopg[binary]` (asyncpg bugué sur Py 3.14 Windows), port Docker PostgreSQL 5432 → 5433 (service `postgresql-x64-16` natif Windows squattait 5432 en dual binding), `POSTGRES_HOST_AUTH_METHOD=trust` ajouté en dev (à retirer en prod Linux), `connect_args={"timeout": 5}` → `{"connect_timeout": 5}` (kwarg spécifique psycopg v3). Windows `SelectorEventLoopPolicy` forcé dans `main.py` et `migrations/env.py` par précaution. | `pyproject.toml`, `.env`, `docker/docker-compose.yml`, `app/core/database/postgres.py`, `app/main.py`, `migrations/env.py` |
| 2026-04-18 | **Audit P0 — 8 chantiers sécurité/infra fermés.** (1) Auth obligatoire sur `/chat/stream` et `/image/generate` via `Depends(get_current_user)`. (2) Scrubber de secrets dans `validation_exception_handler` (clés `password/token/secret/...` → `***REDACTED***` récursif sur dict/bytes/Pydantic errors). (3) Hardening config production : `_enforce_production_safety` model_validator refuse wildcard CORS / app_secret faible / clés JWT vides / debug=True / db_echo=True en prod. (4) Observabilité : `core/observability/` avec `configure_logging` (JSON prod / Console dev, processors structlog + contextvars) et `TraceIdMiddleware` (X-Request-ID propagé, access log avec duration_ms, contextvars cleared). (5) Split health : `/healthz` liveness (pas de check externe) et `/ready` readiness (503 si DB/Redis KO), `/health` alias rétrocompat. (6) Dockerfile multi-stage : builder `python:3.12-slim` + uv 0.5.14, runtime slim + libpq5 + non-root UID 1001, HEALTHCHECK /healthz, uvicorn --proxy-headers, `.dockerignore`. (7) Worker arq : `workers/worker.py` (WorkerSettings + RedisSettings + cron 03:17 UTC + lifecycle hooks) et `workers/auth_tasks.py` (`cleanup_refresh_tokens` purge expirés > 1j et révoqués > 7j). (8) Tests pytest : 9 tests verts (`test_auth_hardening.py` — healthz, ready, password policy, scrub dict/bytes/errors, prod config rejection wildcard / acceptation valide), `conftest.py` avec env vars test non-routables. Fix structlog `add_logger_name` incompatible PrintLoggerFactory → utilisé `processors.add_log_level`. Dépendances : `arq>=0.26,<1`. | `app/main.py`, `app/config.py`, `app/core/errors/handlers.py`, `app/core/observability/__init__.py`, `app/core/observability/logging.py`, `app/core/observability/trace.py`, `docker/Dockerfile`, `.dockerignore`, `workers/worker.py`, `workers/auth_tasks.py`, `workers/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_auth_hardening.py`, `pyproject.toml` |
| 2026-04-19 | **Synchronisation CLAUDE.md ↔ réalité.** Section 7 mise à jour : Dockerfile ✅, `structlog + trace_id` ✅ (séparé d'OpenTelemetry resté ❌), worker arq ✅, production safety guard ✅, scrubber secrets ajouté à la ligne error handlers, ligne tests sécurité 🔧 ajoutée. Aucun code modifié — purement documentaire pour combler le drift relevé lors de l'audit de fond du 2026-04-19. | `CLAUDE.md` |
| 2026-04-19 | **Phase A étape 5 — `app/seed.py`.** Script idempotent qui upsert deux comptes démo (`free@nexya.ai` / `DemoFree2026!`, `pro@nexya.ai` / `DemoPro2026!` plan pro 1 an). Mots de passe respectent la politique RegisterRequest (≥12, maj/min/chiffre/spécial). Refuse explicitement en prod (`settings.is_production` → `sys.exit(2)`). Tronque bcrypt à 72 bytes par cohérence avec `auth.service._hash_password`. Lancement : `python -m app.seed`. Caractères ASCII uniquement dans les `print` (cp1252 Windows ne supporte pas U+2501). | `app/seed.py` |
| 2026-04-21 | **Phase 4 Chat persisté — Lot 5 livré (worker auto-titre + signalements).** (1) **`workers/chat_tasks.py`** (nouveau, ~260 lignes) — `enqueue_title_generation(conversation_id)` : helper module-level (volontairement, pour faciliter le monkeypatch dans les tests d'intégration) avec pool arq paresseux `_get_arq_pool()` et imports arq (`RedisSettings`, `create_pool`) déplacés à l'intérieur de la fonction pour ne pas exiger la dépendance au moment de l'import du module (tests qui patchent l'enqueue n'ont plus besoin d'arq installé) ; échec silencieux `log.warning + return` si Redis flap — la génération de titre reste cosmétique, jamais bloquante. `generate_conversation_title(ctx, conversation_id)` : worker task arq, charge la conv + les 6 derniers messages `completed` (ORDER BY `created_at DESC, id DESC` puis `reverse()` Python pour ordre chronologique), garde-fou `len(rows) < 2` (au moins 1 user + 1 assistant), double-check défensif `title_generated_at IS NULL AND title IS NULL` et `deleted_at IS NULL` (résistance à un re-livraison de job), appel `LlmRouter.resolve("general")` → Gemini Flash (~$0.00005/titre, worst-case ~$475/mois à 950 k users), `_sanitize_title()` (strip guillemets typographiques `"'«»""`, rstrip ponctuation finale `.!?:;,`, troncature `TITLE_MAX_CHARS=60` avec `…`), persistance atomique `UPDATE Conversation SET title, title_generated_at, updated_at WHERE id = ? AND title_generated_at IS NULL` (la clause sentinelle dans le WHERE rend l'UPDATE naturellement idempotent même si deux workers tournent en parallèle). Exceptions IA swallowed → `{skipped: true, reason: 'llm_failed'}` (le titre n'est jamais critique). Constantes `TITLE_MAX_TOKENS=40`, `TITLE_PROVIDER_KEY="general"`. (2) **`workers/worker.py`** — ajout de `generate_conversation_title` dans `functions = [cleanup_refresh_tokens, generate_conversation_title]`. (3) **`app/features/chat/service.py`** — classe `ReportService` avec deux statiques : `_get_owned_message(message_id, user_id, db)` : JOIN `Message` × `Conversation` en **une seule requête** (`SELECT m.* FROM messages m JOIN conversations c ON m.conversation_id = c.id WHERE m.id = ? AND c.user_id = ? AND m.deleted_at IS NULL AND c.deleted_at IS NULL`) — seul rempart IDOR, lève `ResourceNotFoundException("Message")` (404 et **jamais 403**, pour ne pas confirmer l'existence du message au user qui ne le possède pas) ; `create_report(user, body, db)` : owner check via `_get_owned_message`, `INSERT AbuseReport(user_id, message_id, conversation_id=message.conversation_id, reason, detail)` (dénormalisation `conversation_id` depuis le message → évite un second SELECT), try/except sur `db.commit()` : `IntegrityError` (UNIQUE `user_id, message_id`) → `db.rollback()` + `DuplicateReportException` 409 `DUPLICATE_REPORT` (pattern TOCTOU-safe, pas de pré-SELECT anti-concurrence), `db.refresh(report)` puis return. (4) **`app/features/chat/router.py`** — endpoint `POST /chat/reports` (201 + `NexyaResponse[AbuseReportResponse]`) : `await rate_limit_abuse_reports(current_user.id)` **avant** tout accès DB (budget-first), délègue ensuite à `ReportService.create_report()`. Modif de `_finalize_in_fresh_session()` pour le hook auto-titre : après `finalize_assistant_stream()`, relecture `Conversation` et test `status=='completed' AND message_count >= _TITLE_AUTOGENERATE_THRESHOLD (4) AND title IS NULL AND title_generated_at IS NULL`, puis `await enqueue_title_generation(conv_id)` **hors** du `async with AsyncSessionLocal()` (on ne tient pas la session pendant un appel réseau Redis). Seuil `>= 4` délibéré (et non `== 4`) : si l'enqueue échoue sur un 2ᵉ tour, un 3ᵉ tour retentera — la sentinelle `title_generated_at` protège des doublons. Constante module-level `_TITLE_AUTOGENERATE_THRESHOLD = 4`. (5) **`app/core/security/rate_limiter.py`** — ajout `USER_RATE_LIMIT_PREFIX = "rate:user:"`, `check_user_rate_limit(user_id, action, max_requests, window_seconds, *, on_exceeded=RateLimitAbuseException)` : sliding-window Redis `INCR` + `EXPIRE` atomique au premier hit (pattern générique réutilisable au-delà des abus), helper `rate_limit_abuse_reports(user_id)` (10/h/user, clé `rate:user:abuse_report:{uid}`). Sémantique distincte de `RATE_LIMIT_IP` : un signalement est toujours authentifié, donc on trace à l'user_id, pas à l'IP (impossible pour un user NAT carrier mobile d'échapper à sa limite en changeant d'IP). (6) **`app/core/errors/exceptions.py`** — `DuplicateReportException` (409 `DUPLICATE_REPORT`) + `RateLimitAbuseException(retry_after=3600)` (429 `RATE_LIMIT_ABUSE`, `data={retry_after}` lisible côté Flutter). (7) **`app/core/errors/handlers.py`** — **fix collatéral** : `nexya_exception_handler` propage désormais `exc.data` dans `NexyaResponse.data` (une ligne ajoutée, `data=exc.data`). Auparavant, `RateLimitExceededException(reset_at=...)` et `RateLimitIPException(retry_after=...)` stockaient bien leur payload dans l'instance d'exception mais il était **perdu** dans la conversion vers `NexyaResponse` — le Flutter ne recevait jamais `retry_after` alors que le contrat le promettait. Mis en lumière par le test 429 du Lot 5 (`body["data"]["retry_after"] == 1800` → `None`). (8) **`tests/test_abuse_reports.py`** (nouveau, ~330 lignes, **9 tests verts**) : `ReportService` unit × 3 (happy-path avec `conversation_id` correctement dénormalisé depuis le message, propagation 404 quand `_get_owned_message` lève, traduction `IntegrityError → DuplicateReportException` avec rollback), router × 6 (201 + enveloppe `NexyaResponse[AbuseReportResponse]`, 422 Pydantic sur `reason` hors Literal, 422 Pydantic sur `message_id` non-UUID, 404 cascadé depuis `ResourceNotFoundException`, 409 cascadé depuis `DuplicateReportException`, 429 avec `retry_after=1800` dans `data` + service non appelé). Fixture `client` avec `app.dependency_overrides[get_current_user] = fake_user` + `get_db = fake_db` + helper `_patch_rate_limit_noop` pour les tests qui veulent passer le rate limiter. (9) **`tests/test_chat_stream_persisted.py`** (étendu) — `_FakeAsyncContextSession` acceptée kwarg `conversation` + méthode `get = AsyncMock(return_value=conversation)` (le nouvel hook title appelle `db.get(Conversation, conv_id)` dans `_finalize_in_fresh_session`). Helper `_setup_persisted_stream(monkeypatch, conv, conv_after_finalize)` qui installe les mocks AI + moderation + budget + le session factory 2-temps (conv au start, conv post-finalize au hook). 4 nouveaux tests : enqueue si `message_count=4 AND title_generated_at IS NULL`, skip si `message_count=2` (below threshold), skip si `title_generated_at` déjà posé, skip si `status != completed` (done_reason='error'). **Suite complète : 63/63 tests verts** (9 auth hardening + 7 service Lot 2 + 16 router Lot 3 + 22 Lot 4+5 stream persisté + 9 abuse reports). **Décisions architecturales** : (a) enqueue **hors** de la session SQLAlchemy — on ne tient pas une connexion DB pendant un appel réseau Redis ; (b) JOIN en une requête pour l'owner check report — évite le coût + la race condition d'un double SELECT ; (c) `IntegrityError → 409` plutôt que pré-SELECT anti-doublon — la contrainte UNIQUE Postgres est la source de vérité, un pré-check est TOCTOU-sujet ; (d) code d'erreur user-scoped `RATE_LIMIT_ABUSE` distinct de `RATE_LIMIT_IP` — les deux cas demandent une UX différente côté Flutter (l'IP suggère "réessaie ailleurs", l'abuse suggère "tu spammes le bouton Signaler") ; (e) lazy import arq pour ne pas casser `pytest` sans arq installé. | `workers/chat_tasks.py`, `workers/worker.py`, `app/features/chat/service.py`, `app/features/chat/router.py`, `app/core/security/rate_limiter.py`, `app/core/errors/exceptions.py`, `app/core/errors/handlers.py`, `tests/test_abuse_reports.py`, `tests/test_chat_stream_persisted.py` |
| 2026-04-21 | **Phase 4 Chat persisté — Lot 1 livré (fondation data).** (1) **Modèles ORM** (`app/features/chat/models.py`, ~200 lignes) : `Conversation` (12 cols, dénormalisation `last_message_at` + `message_count`, soft-delete `deleted_at`, `title_generated_at` sentinelle one-shot auto-titre, index composite `(user_id, deleted_at, last_message_at)` + index partiel `WHERE is_favorite = true AND deleted_at IS NULL`), `Message` (16 cols, TEXT pour `content`, `NUMERIC(10, 6)` pour `cost_usd`, CHECK contraintes sur `role` et `status`, index cursor-stable `(conversation_id, created_at, id)`), `AbuseReport` (12 cols, `UNIQUE (user_id, message_id)` anti-doublon, CHECK sur `reason` et `status`, 2 indexes — queue admin + historique user). Relations : `Conversation.messages` en `lazy="noload"` + `passive_deletes=True` (aucun chargement implicite des messages, cascade déléguée à Postgres `ON DELETE CASCADE`), pas de relation inverse `User.conversations` pour éviter le N+1. (2) **Schémas Pydantic v2** (`app/features/chat/schemas.py`, ~210 lignes) : 11 schémas — `ConversationCreate`/`Update`/`Response`/`ListItem`, `MessageResponse`, `MessagesPage` (cursor-based), `ChatStreamRequest` (avec compat descendante `history=[...]` et `conversation_id` optionnel), `ChatStreamInlineMessage`, `ChatStopRequest`, `ImageGenerateRequest`, `AbuseReportCreate`/`Response`. Types enum-like via `Literal[...]` alignés 1:1 sur les CHECK SQL (`MessageRole`, `MessageStatus`, `AbuseReason`, `AbuseStatus`). Cap applicatif `_MESSAGE_MAX_CHARS = 32_000` sur `content` (DB reste TEXT sans limite). (3) **Migration Alembic 002** (`migrations/versions/002_create_chat_tables.py`, ~125 lignes) : `revision = "002_chat"`, `down_revision = "001_auth"`, `upgrade()` crée les 3 tables + 5 indexes (dont l'index partiel Postgres avec `postgresql_where=sa.text(...)`) + 6 CHECK constraints + FK `ondelete="CASCADE"` sur les 4 FK, `downgrade()` DROP dans l'ordre inverse (abuse_reports → messages → conversations). (4) **Enregistrement Alembic** (`migrations/env.py`) : ligne ajoutée `from app.features.chat.models import AbuseReport, Conversation, Message` pour `Base.metadata`. Vérifications automatiques : `python -m py_compile` OK sur les 4 fichiers, import effectif dans `Base.metadata` OK (3 tables, 5 indexes, 6 CHECK, 11 schémas Pydantic validés). **Reste Lots 2-5** : service (CRUD + helper cross-user isolation), router (10 endpoints), refactor `/chat/stream` persisté, worker arq auto-titre, `POST /reports` + rate limit abuse. | `app/features/chat/__init__.py`, `app/features/chat/models.py`, `app/features/chat/schemas.py`, `migrations/versions/002_create_chat_tables.py`, `migrations/env.py` |
| 2026-04-21 | **Phase 4 Chat persisté — Lot 3 livré (router CRUD + tests).** (1) **`app/features/chat/router.py`** (~180 lignes) — `APIRouter(prefix="/chat", tags=["chat"])`, 6 endpoints qui délèguent à `ConversationService` sans une seule ligne de logique métier : `POST /chat/conversations` (201 + `ConversationResponse`), `GET /chat/conversations` avec query params `cursor` (`max_length=256`), `limit` (`ge=1, le=50`), `is_archived` (défaut `false`), `is_favorite` (tri-état), `GET /chat/conversations/{id}` (404 IDOR-safe), `PATCH /chat/conversations/{id}` (partiel via `ConversationUpdate`), `DELETE /chat/conversations/{id}` (soft → 204 `Response`, convention REST pour suppressions idempotentes), `GET /chat/conversations/{id}/messages` (owner check + cursor ASC). Conversion ORM → Pydantic via `.model_validate(...)` dans le router (le service parle ORM, le router parle Pydantic). Guards `Depends(get_current_user)` + `Depends(get_db)` sur toutes les routes. (2) **`app/features/chat/schemas.py`** — ajout du schéma `ConversationsPage` (Pydantic) : `items: list[ConversationListItem]` + `next_cursor: str | None`. (3) **`app/features/chat/service.py`** — renommage du DTO interne `ConversationsPage` → `ConversationsPageOrm` pour cohérence avec `MessagesPageOrm` déjà présent (Pydantic owns le nom simple, l'ORM prend le suffixe `Orm`). (4) **`app/main.py`** — import `from app.features.chat.router import router as chat_router` + `app.include_router(chat_router)` après `auth_router` (pas d'impact sur `/chat/stream` servi directement par `main.py`, les préfixes ne collisionnent pas). (5) **`tests/test_conversations_crud.py`** (~280 lignes) — **16 tests router** via `fastapi.testclient.TestClient` + `app.dependency_overrides` pour surcharger `get_current_user` (fake `MagicMock(spec=User)` avec `id=UUID`) et `get_db` (yield `MagicMock()` — session non consultée puisque service monkeypatché). `monkeypatch.setattr(ConversationService, "create", AsyncMock(return_value=conv))` par test → on vérifie le câblage routeur ↔ service, les statuts HTTP et la forme `NexyaResponse[T]`, sans démarrer Postgres. Couverture : happy-path × 6 (201, 200 × 3, 204, 200), isolation cross-user 404 × 4 (`get`/`patch`/`delete`/`list_messages` → service lève `ResourceNotFoundException` → handler global renvoie `NexyaResponse(success=False, code="RESOURCE_NOT_FOUND")`), curseur forgé → 422 `VALIDATION_ERROR` (service lève `ValidationException` remontée par handler), UUID malformé → 422 Pydantic (rejeté avant le service), titre whitespace-only → 422 (validator `title_not_only_whitespace`), `limit=500` → 422 (plafond `le=50` FastAPI), forward des filtres `is_archived` / `is_favorite` au service vérifié via `mock.await_args.kwargs`. **Suite complète : 32/32 tests verts** (9 auth hardening + 7 service Lot 2 + 16 router Lot 3), **aucune régression**. **Décisions** : préfixe `/chat/conversations` (et non `/conversations` à la racine) → regroupement sémantique sous le même namespace que `/chat/stream`, qui migrera aussi vers le router au Lot 4 ; `DELETE` → 204 + `Response` vide (pas `NexyaResponse` : soft-delete idempotent sans payload utile) ; pagination plafonnée `limit=50` côté FastAPI pour rejeter `limit=500` avant même d'atteindre le service (défense en profondeur). **Constat Règle F** : `nexya_front_end/lib/features/chat/data/chat_remote_datasource.dart` n'expose aujourd'hui QUE `streamChat()` et `generateImages()` — les 6 méthodes CRUD côté Dart restent à ajouter quand le Flutter attaquera l'écran Historique (backend-first délibéré, pas de blocage). **Reste Lots 4-5** : refactor `/chat/stream` persisté (placeholder `Message(role='assistant', status='streaming')` → finalisation atomique `completed`/`failed`/`cancelled` avec `_bump_counters` dans la même transaction), worker arq `generate_conversation_title` one-shot, `POST /reports` + rate limit abuse. | `app/features/chat/router.py`, `app/features/chat/schemas.py`, `app/features/chat/service.py`, `app/main.py`, `tests/test_conversations_crud.py` |
| 2026-04-21 | **Phase 4 Chat persisté — Lot 2 livré (service).** (1) **`app/features/chat/service.py`** (~320 lignes) — classe `ConversationService` avec méthodes `@staticmethod` : `_get_owned_conversation(conv_id, user_id, db)` (seul rempart IDOR — lève `ResourceNotFoundException` 404, **jamais** 403, sur mismatch `user_id` ou `deleted_at IS NOT NULL`), `create` (commit), `list_for_user` (keyset `COALESCE(last_message_at, created_at) DESC, id DESC` — COALESCE pour éviter les tuples NULL non-comparables en Postgres ; filtres `is_archived` défaut `False` + `is_favorite` tri-état ; `N+1` pour calculer `next_cursor`), `get_by_id`, `update` (partiel `exclude_unset=True`, no-op si payload vide, interdit de changer `expert_id`), `soft_delete` (`deleted_at = NOW()`), `list_messages` (owner check + keyset ASC `(created_at, id) > (cursor_ts, cursor_id)` — index `idx_messages_conv_time` aligné), `_bump_counters` (UPDATE atomique `SET message_count = message_count + 1, last_message_at = NOW()` — **sans commit**, appelé par Lot 4 dans la transaction du stream pour atomicité INSERT↔incrément). Helpers module-level `_encode_cursor` / `_decode_cursor` (base64url de `{iso}|{uuid}`, lève `ValidationException` 422 sur quatre formes de corruption : base64 cassé, non-ASCII, séparateur absent, ISO ou UUID non parsable). Constantes `_DEFAULT_LIMIT=20`, `_MAX_LIMIT=50` + `_clamp_limit()`. DTO internes `ConversationsPage` / `MessagesPageOrm` (dataclass frozen/slots) pour transiter ORM vers le router. (2) **`app/core/errors/exceptions.py`** — ajout `ValidationException(NexYaException)` (code `VALIDATION_ERROR`, HTTP 422) distincte du handler Pydantic global : utilisable par les services pour rejeter un invariant métier en cours de traitement. (3) **`tests/test_conversations_service.py`** (~120 lignes) — 4 tests unitaires sans Postgres : cursor round-trip exact (datetime+UUID), 4 cas paramétrés de curseur malformé → `ValidationException`, `_get_owned_conversation` rend la conv pour le propriétaire, `_get_owned_conversation` lève `ResourceNotFoundException` (code `RESOURCE_NOT_FOUND`, 404) pour un non-propriétaire. Mocks `AsyncMock` + `MagicMock` sur `AsyncSession.execute`. Suite complète : **16/16 tests verts** (9 auth hardening + 7 conversations service, aucune régression). **Décisions architecturales** : service retourne de l'ORM, le router (Lot 3) fera `.model_validate(...)` et l'emballage `NexyaResponse` ; les méthodes CRUD publiques commitent, `_bump_counters` ne commit pas ; `COALESCE(last_message_at, created_at)` en clé de tri au lieu de `NULLS LAST` pour la compatibilité keyset. **Reste Lots 3-5** : router + tests CRUD, refactor `/chat/stream` persisté avec placeholder assistant, worker arq auto-titre + `POST /reports` + rate limit abuse. | `app/features/chat/service.py`, `app/core/errors/exceptions.py`, `tests/test_conversations_service.py` |
| 2026-04-21 | **Phase 4 Chat persisté — Lot 4 livré (`/chat/stream` persisté).** (1) **`app/ai/runtime.py`** (nouveau, ~60 lignes) — module qui casse la dépendance circulaire `main.py ↔ features/chat/router.py`. Expose `get_ai_router()` et `get_stream_handler()` (construction lazy, singletons module-level) + `reset_runtime_for_tests()`. Tout le code applicatif passe désormais par ce module pour accéder aux singletons Couche IA. (2) **`app/ai/streaming.py`** — ajout d'un champ optionnel `metrics: StreamMetrics \| None = None` à `StreamContext`. Le `StreamHandler` réutilise la `StreamMetrics` passée par le router si fournie, sinon en crée une nouvelle. Le router accède au même objet après la fin du stream pour lire `provider` / `model` / `usage` / `cost_usd` finaux et les persister — sans modifier la sémantique de yield ni l'interface publique. (3) **`app/features/chat/service.py`** — 4 nouvelles méthodes statiques : `ensure_conversation_for_stream(conversation_id, user, db, *, expert_id_hint=None)` (crée une conv vide si `None`, sinon `_get_owned_conversation`), `load_context_messages(conversation, db, limit=30)` (charge DESC avec `status == "completed"` puis inverse pour ordre chronologique), `start_stream_turn(conversation, user_text, db)` (insère user + placeholder assistant `status='streaming'` dans la même transaction, appelle `_bump_counters(delta=2)`, commit), `finalize_assistant_stream(...)` (valide le status parmi `{completed, failed, cancelled}`, convertit `cost_usd` float en `Decimal(str(...))` pour respecter `NUMERIC(10,6)`, UPDATE `Message` et `Conversation.last_message_at`, commit). `_bump_counters` étendu d'un kwarg `delta: int = 1`. (4) **`app/features/chat/router.py`** (réécriture, ~500 lignes) — ajoute `POST /chat/stream` et `POST /chat/stop`, les 6 endpoints CRUD restent intacts. Dispatch 3-modes : legacy stateless (`conversation_id=None` + `history=[...]` → aucune écriture, rétrocompat Flutter), nouvelle conv persistée (création + `start_stream_turn`), conv existante persistée (owner check + `load_context_messages`). Wrapper `_persisted_stream()` consomme le générateur du `StreamHandler`, parse chaque événement via `_observe_sse_event()` (split `event:` / `data:`, skip `:` comments), accumule `content_parts`, mémorise `done_reason` et `error_code`, et en `finally` lance `asyncio.shield(_finalize_in_fresh_session(...))` pour garantir la persistance même en cas de déconnexion client. `_DONE_REASON_TO_STATUS = {"stop": "completed", "cancelled": "cancelled", "error": "failed"}` aligne le mapping SSE → SQL CHECK. Dataclass `_StreamOutcome` (accumulateur mutable). Header `X-Conversation-Id` posé uniquement sur les modes persistés. Budget + modération vérifiés avant toute écriture DB. (5) **`app/main.py`** — nettoyage : suppression des singletons locaux `_AI_ROUTER` / `_STREAM_HANDLER`, suppression de `/chat/stream` / `/chat/stop` / `ChatRequest` / `ChatMessage` / `ChatStopRequest` / `_coerce_role` (migrés vers `features/chat/router.py`). `main.py` passe de ~500 à ~260 lignes, ne garde que `lifespan` + health endpoints + `/image/generate` + import des singletons depuis `app.ai.runtime`. (6) **`tests/test_chat_stream_persisted.py`** (nouveau, ~450 lignes, **18 tests verts**) — parsing SSE × 5 (deltas chunk, done, error+done, keepalive ignoré, JSON malformé), mapping `_DONE_REASON_TO_STATUS`, service `ensure_conversation_for_stream` × 2 (nouvelle conv / conv existante), `start_stream_turn` (2 inserts + `delta=2` + commit), `finalize_assistant_stream` × 3 (happy-path, rejet de status invalide, conversion float→Decimal), `/chat/stop` (monkeypatch `mark_cancelled`), `/chat/stream` legacy stateless (pas de persistance), persisted happy-path (status `completed`), persisted error (status `failed` + `error_code`), modération bloquée → 400 `CONTENT_FILTERED`, message vide → 422. Helpers `_install_ai_mocks()` + `_FakeBudgetTracker` + `_FakeModerationService` + `_install_fake_stream_handler` qui yielde des strings SSE pré-cannées. Correctif final : `log.warning("chat.stream.sse_parse_failed", event=...)` entrait en collision avec le kwarg réservé de structlog, renommé en `raw=`. **Suite complète : 50/50 tests verts** (9 auth hardening + 7 service Lot 2 + 16 router Lot 3 + 18 Lot 4), zéro régression. Reste Lot 5 : worker arq auto-titre + `POST /reports` + rate limit abuse. | `app/ai/runtime.py`, `app/ai/streaming.py`, `app/features/chat/service.py`, `app/features/chat/router.py`, `app/main.py`, `tests/test_chat_stream_persisted.py` |
| 2026-04-21 | **F2.0 Chat — Corbeille + filtre expert (backend).** (1) **`app/features/chat/service.py`** — `list_for_user` gagne un kwarg `expert_id: str \| None = None` qui ajoute `Conversation.expert_id == expert_id` dans la clause WHERE (filtre optionnel pour les écrans Flutter « Discussions par expert »). Ajout du helper privé `_get_owned_conversation_in_trash(conv_id, user_id, db)` — symétrique de `_get_owned_conversation`, exige `deleted_at IS NOT NULL` — seul rempart pour les actions de corbeille (isolation stricte entre monde actif et monde corbeille). Trois nouvelles méthodes statiques : `list_trash_for_user(user, db, *, cursor, limit, expert_id)` — keyset sur `deleted_at DESC, id DESC` (et non `COALESCE(last_message_at, created_at)` — la corbeille se tri par récence de suppression, pas d'activité), filtre `deleted_at IS NOT NULL` + owner ; `restore(conv_id, user, db)` — efface `deleted_at` (ne bump PAS `last_message_at` pour ne pas perturber le classement actif) ; `permanent_delete(conv_id, user, db)` — SQL DELETE physique, cascade via `ON DELETE CASCADE` sur messages + abuse_reports, exige explicitement l'état corbeille (two-step flow garanti — pas de purge directe d'une conv active). (2) **`app/features/chat/schemas.py`** — `ConversationResponse` et `ConversationListItem` gagnent `deleted_at: datetime \| None = None`. `None` sur les endpoints actifs (la clause SQL `deleted_at IS NULL` garantit qu'aucune fuite), peuplé sur `/trash` + `/restore`. (3) **`app/features/chat/router.py`** — ajout `expert_id: str \| None = Query(default=None, min_length=1, max_length=32)` à `list_conversations` (forwardé au service). Trois nouveaux endpoints : `GET /chat/conversations/trash` (listé **avant** `/conversations/{id}` pour que FastAPI ne parse pas `"trash"` comme UUID → garde verrouillée par un test dédié), `POST /chat/conversations/{id}/restore` (200 + `ConversationResponse` avec `deleted_at=null`), `DELETE /chat/conversations/{id}/permanent` (204 + `Response` vide). Docstrings étendues au header module sur les 3 nouveaux endpoints. (4) **`tests/test_conversations_crud.py`** — helper `_make_fake_conversation` gagne un kwarg `deleted_at`. 11 nouveaux tests (suite 16 → 27 verts) : forward du filtre `expert_id` dans la liste active × 2, corbeille happy-path (avec `deleted_at` peuplé), corbeille forward `expert_id`, garde anti-régression précédence de route `/trash` vs `/{id}`, restore happy-path (200 + `deleted_at=null`), restore 404 IDOR-safe, restore UUID malformé 422, permanent_delete 204, permanent_delete 404 IDOR-safe, `deleted_at` exposé dans le contrat `ConversationResponse`. Suite complète **27/27 verts** en 196 s (timings Windows + psycopg). **Décisions architecturales** : (a) endpoint trash dédié plutôt que flag `?include_deleted=true` — sémantique de tri différente (récence de suppression vs récence d'activité) + contrat simpler pour le Flutter (écran `trash_screen.dart` attend une collection isolée) ; (b) actions REST `POST /{id}/restore` et `DELETE /{id}/permanent` plutôt qu'étendre PATCH/DELETE — un verbe par intention métier, pas de sémantique ambiguë « `PATCH deleted_at=null` veut-il dire restaurer ou effacer ? » ; (c) helper symétrique `_get_owned_conversation_in_trash` — empêche l'accident où une action de corbeille toucherait une conv active (et réciproquement pour les endpoints actifs) ; (d) `restore()` ne bump pas `last_message_at` — la restauration est une récupération, pas une réactivation artificielle du classement ; (e) `permanent_delete()` utilise un vrai `DELETE` SQL + `ON DELETE CASCADE` plutôt qu'une boucle applicative — atomique en une transaction Postgres, zéro N+1. (5) **`CLAUDE.md` nexya_backend** — §7 : table chat mise à jour (3 nouveaux endpoints F2.0 ✅ + ligne Lot F2.0 synthétique). §15 : cette entrée. **`CLAUDE.md` nexya_front_end** — §12 : référence API mise à jour (3 nouveaux endpoints + query `expert_id`). | `app/features/chat/service.py`, `app/features/chat/schemas.py`, `app/features/chat/router.py`, `tests/test_conversations_crud.py`, `CLAUDE.md` (nexya_backend), `../nexya_front_end/CLAUDE.md` |
| 2026-04-21 | **Couche IA backend — 7 briques Tier 1 livrées + refactor des endpoints.** (1) **Providers** : ABC `LlmProvider` + types neutres (`ChatMessage`, `ChatChunk`, `ChatCompletionRequest`, `ImageGenerationRequest`, `ChatUsage`) + hiérarchie d'erreurs typées (`ProviderError` / `Unavailable` / `RateLimit` / `Auth` / `ContentFiltered` / `InvalidRequest`, flag `retryable`). `GeminiProvider` réel (chat streaming + Imagen 3). Stubs OpenAI / Anthropic / Qwen conformes à l'ABC, prêts pour câblage SDK. (2) **ContextBuilder via `experts.py`** : 11 `ExpertConfig` (`general` + 10 experts alignés sur `ExpertDomain.name` Flutter), tier modèle Flash par défaut, Pro pour Sciences/Ingénierie/Médecine/Légal, disclaimers métiers (médecine, droit). (3) **LlmRouter** : `resolve(expert_id) → ChatResolution`, `build_chain(expert_id) → list[ChatResolution]`, `resolve_image(expert_id) → ImageResolution`. Factory `build_default_router()` câble Gemini réel + 3 stubs + Imagen. Filtre les providers non enregistrés avec warning, log si modèle non supporté. Validé : 11 experts résolus, expert inconnu → fallback `general`, `studio` retourne chaîne chat vide (image-only). (4) **ModerationService** : OpenAI `omni-moderation-latest`, fail-open 3 s sur erreurs transport, désactivable si `settings.openai_api_key` vide (warning log unique), singleton + `close_moderation_service()` pour lifespan. (5) **BudgetTracker Redis** : 4 méthodes (`check_and_consume_chat`, `check_and_consume_image`, `check_and_consume_ip_burst`, `check_and_consume_model`). Defaults : 200 chat/user/jour, 50 img/user/jour, 20 req/IP/min, cap modèle 100k/jour. Atomique `INCR` puis `DECR` rollback si dépassé → `RateLimitExceededException` avec `reset_at=next_midnight_utc()`. Fail-open sur erreurs Redis. Clés UTC : `budget:user:{uid}:chat:{YYYY-MM-DD}`, `budget:ip:{ip}:{YYYY-MM-DDTHH:MM}`, `budget:model:{m}:{YYYY-MM-DD}`. (6) **Retry exponentiel + jitter** : `RetryPolicy(max_attempts=3, base_delay=0.5s, max_delay=5s, jitter_ratio=0.25)`. Critique streaming : retry uniquement AVANT le 1ᵉʳ chunk (sinon texte dupliqué). Honore `ProviderRateLimitError.retry_after_seconds`. `asyncio.CancelledError` toujours propagé. (7) **CircuitBreakerRegistry** : par `(provider, model)`, machine d'état `CLOSED→OPEN→HALF_OPEN`, in-memory `RLock` thread-safe. Defaults : 5 échecs / 30 s cooldown / 1 essai sondage. Erreurs non-retryables (auth, content_filter) n'ouvrent PAS le circuit (bug NEXYA, pas panne provider). `CircuitOpenError` typée `retryable=False` pour que le router skip vers le fallback suivant. (8) **StreamHandler SSE** : orchestre la chaîne avec retry + breaker + heartbeat. Helpers SSE (`event: chunk` / `: keepalive` 15 s / `event: error` / `event: done`). Annulation duale : `Request.is_disconnected()` toutes les 2 s + clé Redis `chat:cancel:{session_id}` TTL 300 s vérifiée toutes les 1 s. `_interleave_with_heartbeat()` avec sentinelles. Traversée chaîne : `_ChainLinkFailed` → lien suivant, `_ChainCancelled` → `STREAM_CANCELLED`, chaîne épuisée → `LLM_UNAVAILABLE`. Premier chunk préfixé du disclaimer si l'expert en a un. (9) **Observabilité** : `StreamMetrics` accumulateur (user/trace/expert/session/provider/model/timing/chunks/bytes/attempts/fallback/usage/cost/outcome/failure_code), table prix USD/1M tokens (Gemini, OpenAI GPT-4o + o1, Claude 4, Qwen 2.5), `estimate_cost_usd()` → 0 + warning sur modèle inconnu, log unique `ai.chat.completed` à la fin de chaque stream. (10) **Refactor endpoints `app/main.py`** : `/chat/stream` → budget → modération → `StreamHandler.stream()` → `StreamingResponse` avec header `X-Session-Id`. `/chat/stop` (POST) → `mark_cancelled(session_id)`. `/image/generate` → budget → modération → `_AI_ROUTER.resolve_image(expert_id)` → `provider.generate_images()` → base64 + mime_type + provider + model. Helper `_coerce_role` normalise `'ai'` / `'bot'` / `'model'` → `'assistant'`. Singletons module-level `_AI_ROUTER` et `_STREAM_HANDLER`, eager-build dans le lifespan, `close_moderation_service()` au shutdown. 9/9 tests pytest verts (aucune régression). | `app/ai/providers/__init__.py`, `app/ai/providers/base.py`, `app/ai/providers/gemini.py`, `app/ai/providers/openai_provider.py`, `app/ai/providers/anthropic_provider.py`, `app/ai/providers/qwen_provider.py`, `app/ai/experts.py`, `app/ai/router.py`, `app/ai/moderation.py`, `app/ai/budget_tracker.py`, `app/ai/retry.py`, `app/ai/circuit_breaker.py`, `app/ai/streaming.py`, `app/ai/observability.py`, `app/main.py` |
| 2026-04-21 | **Validation end-to-end manuelle Lots 1-5 + F2.0 + fix `MissingGreenlet` post-rollback dans `ReportService.create_report`.** Session de validation `curl` end-to-end contre une vraie stack PostgreSQL+Redis : login `free@nexya.ai`, création conv, stream SSE persisté (`/chat/stream` → status `completed`, ~12,5 s, Gemini 2.5-flash, ~$0.000032), persistance vérifiée (rôles user + assistant + métadonnées coût/provider), corbeille (soft-delete → trash → restore → permanent + cascade SQL), `/chat/stop` (annulation duale validée — header `X-Session-Id` extrait, POST stop → 200 `cancelled:true`, SSE finalise sur `event: done` `data:{"reason":"cancelled"}`, message persisté en `status=cancelled`), `/chat/reports` happy-path (201 + payload complet) **PUIS bug réel découvert sur le doublon** : 500 `INTERNAL_ERROR` au lieu du 409 attendu. Stack trace : `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called` à `service.py:995` dans `user_id=str(user.id)`. **Cause** : après `await db.rollback()` dans le `except IntegrityError`, SQLAlchemy expire toutes les colonnes ORM ; l'accès `str(user.id)` du log déclenchait un lazy-load → `pool_pre_ping` (notre `pool_pre_ping=True` dans `core/database/postgres.py`) → `dbapi_connection.autocommit = True` qui est un **setter sync** sur la connexion psycopg → `await_only()` appelé hors greenlet async → crash. **Fix appliqué dans `app/features/chat/service.py:976-1014`** : capture `user_id_str = str(user.id)` / `message_id_str = str(message.id)` / `conversation_id_str = str(message.conversation_id)` AVANT le `try/commit`, le `except` ne touche plus à aucun attribut ORM expiré post-rollback. Re-test après reload uvicorn : premier report → 201 ✅, doublon → 409 `DUPLICATE_REPORT` + message FR propre ✅. Note : le champ JSON `done_reason` retourné comme `None` dans le diagnostic curl était un artefact de mon `dict.get('done_reason')` — la colonne **n'existe pas** dans le modèle ORM `Message` ni dans `MessageResponse` (par design : `done_reason` vit uniquement comme accumulateur local dans le wrapper `_persisted_stream` du router pour mapper la raison SSE vers le `status` SQL final). Pas de fix nécessaire. **Faux signal annexe** : un mojibake apparent (`d\u00c3\u00a9velopp\u00c3\u00a9`) sur les messages stockés s'est révélé être un artefact de `python -m json.tool` qui sur Windows lit stdin en `cp1252` et mis-decode les bytes UTF-8 valides (`\xc3\xa9` = é) en Latin-1 (`Ã©`) puis les re-sérialise avec `ensure_ascii=True`. Diagnostic via script éphémère `scripts/diag_mojibake.py` (supprimé après) qui interrogeait `octet_length()` / `length()` / `convert_to(content, 'UTF8')` sur le dernier message — char_len=163, byte_len=171 cohérent avec 8 caractères accentués × 2 bytes UTF-8 chacun. La DB est clean. **Aucune régression** sur la suite pytest 63/63 verts. **À investiguer plus tard (non bloquant)** : log `cancel_check_error Timeout reading from 127.0.0.1:6379` observé pendant les streams — la vérification de la clé Redis d'annulation timeout occasionnellement, le stream continue normalement. Probablement un tuning de timeout côté `redis_client.get()` à serrer. | `app/features/chat/service.py`, `CLAUDE.md`, `docs/ROADMAP.md`, `COURS_NEXYA_BACKEND.md` |
