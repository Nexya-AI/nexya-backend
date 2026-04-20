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
python -m app.seed

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
│   ├── seed.py                  # Script peuplement DB (dev uniquement)
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
| Seed data (`python -m app.seed`) | `app/seed.py` | ✅ (2 comptes démo : free@nexya.ai / pro@nexya.ai, idempotent, refusé en prod) |
| Docker Compose dev (app + postgres + redis + minio) | `docker/docker-compose.yml` | ✅ |
| Dockerfile multi-stage (builder uv + runtime non-root) | `docker/Dockerfile`, `.dockerignore` | ✅ |
| Connexion PostgreSQL async | `core/database/postgres.py`, `core/database/base.py` | ✅ |
| Connexion Redis | `core/database/redis.py` | ✅ |
| Migrations Alembic (init + toutes les tables) | `migrations/` | 🔧 (init + Auth ✅) |
| JWT RS256 (encode/decode/refresh/blacklist) | `core/auth/jwt.py`, `core/auth/refresh.py` | ✅ |
| Guards (get_current_user, require_pro) | `core/auth/guards.py` | ✅ |
| Rate limiter (user + IP) | `core/security/rate_limiter.py` | 🔧 (IP ✅, user ❌) |
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
| Endpoint | Fichier | Statut |
|---|---|---|
| `POST /chat` (réponse complète) | `features/chat/router.py` | ❌ |
| `POST /chat/stream` (SSE) | `app/main.py` (provisoire — à migrer dans `features/chat/router.py` Phase 4) | ✅ (refactor via Couche IA : budget → modération → StreamHandler avec heartbeat + annulation + fallback chain) |
| `POST /chat/stop` (annulation via clé Redis) | `app/main.py` (provisoire) | ✅ |
| `POST /chat/{message_id}/feedback` | `features/chat/router.py` | ❌ |

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
| 2026-04-21 | **Couche IA backend — 7 briques Tier 1 livrées + refactor des endpoints.** (1) **Providers** : ABC `LlmProvider` + types neutres (`ChatMessage`, `ChatChunk`, `ChatCompletionRequest`, `ImageGenerationRequest`, `ChatUsage`) + hiérarchie d'erreurs typées (`ProviderError` / `Unavailable` / `RateLimit` / `Auth` / `ContentFiltered` / `InvalidRequest`, flag `retryable`). `GeminiProvider` réel (chat streaming + Imagen 3). Stubs OpenAI / Anthropic / Qwen conformes à l'ABC, prêts pour câblage SDK. (2) **ContextBuilder via `experts.py`** : 11 `ExpertConfig` (`general` + 10 experts alignés sur `ExpertDomain.name` Flutter), tier modèle Flash par défaut, Pro pour Sciences/Ingénierie/Médecine/Légal, disclaimers métiers (médecine, droit). (3) **LlmRouter** : `resolve(expert_id) → ChatResolution`, `build_chain(expert_id) → list[ChatResolution]`, `resolve_image(expert_id) → ImageResolution`. Factory `build_default_router()` câble Gemini réel + 3 stubs + Imagen. Filtre les providers non enregistrés avec warning, log si modèle non supporté. Validé : 11 experts résolus, expert inconnu → fallback `general`, `studio` retourne chaîne chat vide (image-only). (4) **ModerationService** : OpenAI `omni-moderation-latest`, fail-open 3 s sur erreurs transport, désactivable si `settings.openai_api_key` vide (warning log unique), singleton + `close_moderation_service()` pour lifespan. (5) **BudgetTracker Redis** : 4 méthodes (`check_and_consume_chat`, `check_and_consume_image`, `check_and_consume_ip_burst`, `check_and_consume_model`). Defaults : 200 chat/user/jour, 50 img/user/jour, 20 req/IP/min, cap modèle 100k/jour. Atomique `INCR` puis `DECR` rollback si dépassé → `RateLimitExceededException` avec `reset_at=next_midnight_utc()`. Fail-open sur erreurs Redis. Clés UTC : `budget:user:{uid}:chat:{YYYY-MM-DD}`, `budget:ip:{ip}:{YYYY-MM-DDTHH:MM}`, `budget:model:{m}:{YYYY-MM-DD}`. (6) **Retry exponentiel + jitter** : `RetryPolicy(max_attempts=3, base_delay=0.5s, max_delay=5s, jitter_ratio=0.25)`. Critique streaming : retry uniquement AVANT le 1ᵉʳ chunk (sinon texte dupliqué). Honore `ProviderRateLimitError.retry_after_seconds`. `asyncio.CancelledError` toujours propagé. (7) **CircuitBreakerRegistry** : par `(provider, model)`, machine d'état `CLOSED→OPEN→HALF_OPEN`, in-memory `RLock` thread-safe. Defaults : 5 échecs / 30 s cooldown / 1 essai sondage. Erreurs non-retryables (auth, content_filter) n'ouvrent PAS le circuit (bug NEXYA, pas panne provider). `CircuitOpenError` typée `retryable=False` pour que le router skip vers le fallback suivant. (8) **StreamHandler SSE** : orchestre la chaîne avec retry + breaker + heartbeat. Helpers SSE (`event: chunk` / `: keepalive` 15 s / `event: error` / `event: done`). Annulation duale : `Request.is_disconnected()` toutes les 2 s + clé Redis `chat:cancel:{session_id}` TTL 300 s vérifiée toutes les 1 s. `_interleave_with_heartbeat()` avec sentinelles. Traversée chaîne : `_ChainLinkFailed` → lien suivant, `_ChainCancelled` → `STREAM_CANCELLED`, chaîne épuisée → `LLM_UNAVAILABLE`. Premier chunk préfixé du disclaimer si l'expert en a un. (9) **Observabilité** : `StreamMetrics` accumulateur (user/trace/expert/session/provider/model/timing/chunks/bytes/attempts/fallback/usage/cost/outcome/failure_code), table prix USD/1M tokens (Gemini, OpenAI GPT-4o + o1, Claude 4, Qwen 2.5), `estimate_cost_usd()` → 0 + warning sur modèle inconnu, log unique `ai.chat.completed` à la fin de chaque stream. (10) **Refactor endpoints `app/main.py`** : `/chat/stream` → budget → modération → `StreamHandler.stream()` → `StreamingResponse` avec header `X-Session-Id`. `/chat/stop` (POST) → `mark_cancelled(session_id)`. `/image/generate` → budget → modération → `_AI_ROUTER.resolve_image(expert_id)` → `provider.generate_images()` → base64 + mime_type + provider + model. Helper `_coerce_role` normalise `'ai'` / `'bot'` / `'model'` → `'assistant'`. Singletons module-level `_AI_ROUTER` et `_STREAM_HANDLER`, eager-build dans le lifespan, `close_moderation_service()` au shutdown. 9/9 tests pytest verts (aucune régression). | `app/ai/providers/__init__.py`, `app/ai/providers/base.py`, `app/ai/providers/gemini.py`, `app/ai/providers/openai_provider.py`, `app/ai/providers/anthropic_provider.py`, `app/ai/providers/qwen_provider.py`, `app/ai/experts.py`, `app/ai/router.py`, `app/ai/moderation.py`, `app/ai/budget_tracker.py`, `app/ai/retry.py`, `app/ai/circuit_breaker.py`, `app/ai/streaming.py`, `app/ai/observability.py`, `app/main.py` |
