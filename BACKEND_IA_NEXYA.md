# CLAUDE_BACKEND.md — Architecture Backend NEXYA
# Version 3.0 — Corrigée & Complète — 2026-04-03

> Lire ce fichier en entier avant toute session de travail sur le backend.
> Ce document est le miroir backend du CLAUDE.md frontend.
> Langage : Python 3.12 — Framework : FastAPI

---

## 0. Principes fondateurs

- **Le backend décide du modèle IA** — le frontend envoie le contexte, jamais le nom du modèle.
- **SSE-first** — toutes les réponses IA sont streamées. Le mode non-streamé est un fallback.
- **Africa-first** — chaque décision tient compte de la 2G/3G, des paiements locaux, et des appareils low-end.
- **Security by default** — JWT RS256, rate limiting Redis (user + IP), sanitisation stricte, zéro clé API hardcodée.
- **Coût maîtrisé** — chaque token est tracké par utilisateur. Le LlmRouter choisit le modèle le moins cher capable de répondre au besoin.
- **Scalabilité progressive** — architecture pensée pour 0 → 950k utilisateurs sans refonte.

---

## 1. Stack technique

### Core
| Technologie | Usage | Pourquoi |
|---|---|---|
| **FastAPI** (Python 3.12) | Framework API REST + SSE | Async natif, parfait pour SSE, écosystème IA Python-first |
| **SQLAlchemy 2.0 async** | ORM | Async natif, type-safe, migrations Alembic |
| **PostgreSQL 16** | Base de données principale | JSONB pour metadata, pgvector pour mémoire IA, full-text search |
| **Redis 7** | Sessions, cache, rate limit, pub/sub SSE | Ultra-rapide, parfait pour les états temporaires |
| **arq** | Tâches planifiées (Prompt Scheduler) | Async natif Python, léger — pas besoin de Celery pour le MVP |
| **Alembic** | Migrations DB | Versionning de schéma propre |
| **pydantic-settings** | Config par environnement | Type-safe, validation auto des variables d'env |

### IA
| Technologie | Usage |
|---|---|
| **OpenAI SDK** | GPT-4o (chat général), GPT-4o-mini (fallback économique), DALL-E 3 (images), Whisper (STT), TTS |
| **Google Generative AI SDK** | Gemini Pro Vision (analyse images/vidéos, génération vidéo) |
| **Qwen API (Alibaba Cloud)** | Modes Experts + personnalité NYLI (moins cher, très spécialisé) |
| **pgvector** | Vector DB pour mémoire IA long terme (extension PostgreSQL, pas de service séparé) |

### Infrastructure & Stockage
| Technologie | Usage |
|---|---|
| **MinIO / AWS S3** | Fichiers uploadés, médias générés, audio TTS |
| **Nginx** | Reverse proxy, gzip/brotli, HTTP/2 |
| **Docker + Docker Compose** | Containerisation — Compose pour MVP, k3s pour scale |
| **Cloudflare** | CDN, DDoS protection, optimisation réseau Afrique |
| **Firebase Cloud Messaging** | Notifications push Flutter (tokens stockés dans `device_tokens`) |
| **structlog** | Logs structurés JSON corrélés par trace_id |
| **OpenTelemetry** | Tracing distribué end-to-end (chaque requête tracée FastAPI → LLM → DB) |
| **Prometheus** | Exposition des métriques applicatives (/metrics) |
| **Grafana** | Dashboards : latence IA, tokens/coût, erreurs, SSE actives |

### Paiements (Afrique)
| Technologie | Usage |
|---|---|
| **CinetPay / NotchPay** | Agrégateur MVP : Orange Money + MTN + Wave + Airtel en 1 intégration |
| **PaymentProvider ABC** | Interface abstraite extensible — chaque opérateur peut être branché directement si besoin |

---

## 2. Structure des fichiers

```
nexya_backend/
│
├── app/
│   ├── main.py                          # FastAPI app, lifespan, routers
│   ├── config.py                        # Settings (pydantic-settings, env-based)
│   │
│   ├── core/
│   │   ├── auth/
│   │   │   ├── jwt.py                   # Encode/decode JWT (RS256)
│   │   │   ├── guards.py                # Dépendances : get_current_user, require_pro
│   │   │   └── refresh.py               # Refresh token rotation + Redis blacklist
│   │   ├── database/
│   │   │   ├── postgres.py              # AsyncEngine + AsyncSession factory
│   │   │   ├── redis.py                 # Redis pool (aioredis)
│   │   │   └── base.py                  # Base ORM + UUID mixin
│   │   ├── storage/
│   │   │   └── s3.py                    # MinIO/S3 async client (upload, presigned URLs)
│   │   ├── security/
│   │   │   ├── rate_limiter.py          # Sliding window : par user (authentifié) + par IP (auth endpoints)
│   │   │   └── sanitizer.py             # Sanitisation inputs avant tout appel IA
│   │   ├── observability/
│   │   │   ├── tracing.py               # OpenTelemetry — trace chaque requête end-to-end
│   │   │   ├── metrics.py               # Prometheus — compteurs, histogrammes, jauges NEXYA
│   │   │   └── logging.py               # structlog — logs JSON corrélés par trace_id
│   │   └── errors/
│   │       ├── handlers.py              # Global exception handlers FastAPI
│   │       └── exceptions.py            # Nexya exceptions typées
│   │
│   ├── ai/
│   │   ├── engine/
│   │   │   ├── query_engine.py          # Cycle de vie d'un turn IA
│   │   │   ├── stream_handler.py        # SSE streaming + annulation via Redis pub/sub
│   │   │   ├── session_store.py         # StoredSession Redis (TTL 24h) + flush PostgreSQL
│   │   │   └── turn_result.py           # TurnResult + UsageSummary
│   │   ├── providers/
│   │   │   ├── base.py                  # LlmProvider ABC (stream + complete)
│   │   │   ├── openai_provider.py       # GPT-4o, GPT-4o-mini, DALL-E 3, TTS, Whisper
│   │   │   ├── gemini_provider.py       # Gemini Pro Vision + génération vidéo
│   │   │   ├── qwen_provider.py         # Qwen 2.5 (experts + NYLI)
│   │   │   └── local_provider.py        # Fallback offline (futur V2)
│   │   ├── router.py                    # LlmRouter — sélection modèle selon contexte
│   │   ├── context_builder.py           # Injection prompt système par domaine Expert
│   │   ├── cost_tracker.py              # CostTracker par user + quotas + usage_daily
│   │   ├── tool_registry.py             # Registre des tools IA disponibles
│   │   └── tools/
│   │       ├── vision_tool.py           # Analyse image (Gemini Vision)
│   │       ├── web_search_tool.py       # Recherche web (modes experts)
│   │       └── calculator_tool.py       # Calcul (Science/Finance experts)
│   │
│   ├── features/
│   │   ├── auth/
│   │   │   ├── router.py                # POST /auth/login|register|refresh|logout, GET|PUT /user/profile
│   │   │   ├── service.py
│   │   │   ├── schemas.py               # LoginRequest, RegisterRequest, TokenResponse, UserProfile
│   │   │   └── models.py                # User ORM + RefreshToken ORM
│   │   ├── chat/
│   │   │   ├── router.py                # POST /chat, /chat/stream, /chat/stop, /chat/{id}/feedback
│   │   │   ├── service.py               # Orchestre query_engine + cost_tracker
│   │   │   ├── schemas.py               # ChatRequest, ChatResponse, StreamChunk, ChatStopRequest
│   │   │   └── models.py                # Conversation + Message ORM
│   │   ├── history/
│   │   │   ├── router.py                # GET /history, PATCH /history/{id}, DELETE, GET /history/{id}/messages
│   │   │   ├── service.py
│   │   │   └── schemas.py
│   │   ├── projects/
│   │   │   ├── router.py                # CRUD /projects, /projects/{id}/files, /projects/{id}/conversations
│   │   │   ├── service.py
│   │   │   ├── schemas.py
│   │   │   └── models.py                # Project + ProjectFile ORM
│   │   ├── planner/
│   │   │   ├── router.py                # CRUD /tasks, GET /tasks/{id}/results
│   │   │   ├── service.py
│   │   │   ├── schemas.py
│   │   │   ├── models.py                # ScheduledTask + TaskResult ORM
│   │   │   └── worker.py                # arq task : execute_scheduled_task
│   │   ├── voice/
│   │   │   ├── router.py                # GET /voice/list, POST /voice/transcribe|speak
│   │   │   ├── service.py               # Whisper STT + TTS (dynamique)
│   │   │   ├── schemas.py
│   │   │   └── models.py                # Voice ORM
│   │   ├── vision/
│   │   │   ├── router.py                # POST /vision/analyze
│   │   │   ├── service.py               # Gemini Vision
│   │   │   └── schemas.py
│   │   ├── files/
│   │   │   ├── router.py                # POST /file/upload
│   │   │   ├── service.py               # S3 upload + extraction texte PDF
│   │   │   └── schemas.py
│   │   ├── library/
│   │   │   ├── router.py                # GET /library, GET /library/{id}, DELETE /library/{id}
│   │   │   ├── service.py
│   │   │   └── schemas.py
│   │   ├── memory/
│   │   │   ├── router.py                # POST /memory/index|search
│   │   │   ├── service.py               # pgvector embeddings
│   │   │   └── schemas.py
│   │   ├── notifications/
│   │   │   ├── router.py                # GET /notifications, POST /notifications/read, DELETE /notifications/{id}
│   │   │   ├── service.py               # Envoi FCM via device_tokens
│   │   │   ├── schemas.py
│   │   │   └── models.py                # Notification ORM + DeviceToken ORM
│   │   └── subscriptions/
│   │       ├── router.py                # GET /subscriptions/status, POST /checkout|webhook|cancel
│   │       ├── service.py
│   │       ├── schemas.py
│   │       ├── models.py                # Subscription + Payment + UsageRecord + ProcessedWebhook ORM
│   │       └── payments/
│   │           ├── base.py              # PaymentProvider ABC (initiate + verify_webhook)
│   │           ├── cinetpay.py          # CinetPay (agrégateur MVP : Orange/MTN/Wave/Airtel)
│   │           ├── notchpay.py          # NotchPay (alternative CinetPay)
│   │           ├── orange_money.py      # Provider direct Orange Money (si besoin)
│   │           └── mtn.py               # Provider direct MTN (si besoin)
│   │
│   └── shared/
│       ├── schemas.py                   # NexyaResponse[T], PaginatedResponse[T]
│       └── dependencies.py              # Pagination, current_user, db_session
│
├── workers/
│   ├── worker.py                        # arq WorkerSettings
│   ├── scheduled_tasks.py               # Exécution Prompt Scheduler (dispatch_due_tasks)
│   └── media_processor.py               # Traitement async images/vidéos
│
├── migrations/                          # Alembic
│   ├── env.py
│   └── versions/
│
├── tests/
│   ├── unit/
│   │   ├── test_llm_router.py
│   │   ├── test_cost_tracker.py
│   │   └── test_context_builder.py
│   ├── integration/
│   │   ├── test_chat_stream.py
│   │   ├── test_auth.py
│   │   └── test_planner.py
│   └── conftest.py                      # Fixtures : test DB, mock LLM providers
│
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml               # Dev : app + postgres + redis + minio
│   └── docker-compose.prod.yml          # Prod : + nginx + arq worker
│
├── .env.example
├── pyproject.toml                       # uv / pip deps
└── CLAUDE_BACKEND.md                    # Ce fichier
```

---

## 3. Schéma de base de données

### Table `users`
```sql
CREATE TABLE users (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email                    VARCHAR(255) UNIQUE NOT NULL,
    username                 VARCHAR(50) UNIQUE,
    password_hash            VARCHAR(255) NOT NULL,
    display_name             VARCHAR(100),
    avatar_url               VARCHAR(500),
    bio                      TEXT,
    locale                   VARCHAR(10) DEFAULT 'fr',
    timezone                 VARCHAR(50) DEFAULT 'Africa/Douala',
    plan                     VARCHAR(20) DEFAULT 'free',       -- 'free' | 'pro'
    plan_expires_at          TIMESTAMP WITH TIME ZONE,
    voice_id                 UUID REFERENCES voices(id),
    mfa_enabled              BOOLEAN DEFAULT FALSE,
    mfa_secret               VARCHAR(255),
    data_collection_enabled  BOOLEAN DEFAULT TRUE,
    is_active                BOOLEAN DEFAULT TRUE,
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at               TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at               TIMESTAMP WITH TIME ZONE           -- soft delete RGPD
);
```

### Table `refresh_tokens`
```sql
CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  VARCHAR(255) UNIQUE NOT NULL,   -- hash du token (jamais en clair)
    expires_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at  TIMESTAMP WITH TIME ZONE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_refresh_tokens_user ON refresh_tokens(user_id, revoked_at);
```

### Table `device_tokens` ✅ NOUVEAU
```sql
-- Stocke les tokens FCM par appareil pour les notifications push Flutter.
-- Un utilisateur peut avoir plusieurs appareils (téléphone + tablette).
CREATE TABLE device_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token       VARCHAR(500) NOT NULL UNIQUE,   -- token FCM
    platform    VARCHAR(10) NOT NULL,            -- 'android' | 'ios'
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_device_tokens_user ON device_tokens(user_id, is_active);
-- Usage : SELECT token FROM device_tokens WHERE user_id = ? AND is_active = TRUE
-- Appelé par notification_service.send_push() à chaque notification
```

### Table `conversations`
```sql
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id      UUID REFERENCES projects(id) ON DELETE SET NULL,
    title           VARCHAR(255),
    expert_domain   VARCHAR(50),              -- NULL = chat général (Geek-1)
    is_ephemeral    BOOLEAN DEFAULT FALSE,
    is_favorite     BOOLEAN DEFAULT FALSE,
    is_archived     BOOLEAN DEFAULT FALSE,
    is_deleted      BOOLEAN DEFAULT FALSE,
    model_used      VARCHAR(50),
    total_tokens    INTEGER DEFAULT 0,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at      TIMESTAMP WITH TIME ZONE
);

-- expert_domain valeurs : 'computer' | 'science' | 'finance' | 'language' | 'cooking'
--                        | 'studio' | 'engineering' | 'productivity'
-- (synchronisé avec ExpertDomain enum Flutter)

CREATE INDEX idx_conversations_user    ON conversations(user_id, is_deleted, created_at DESC);
CREATE INDEX idx_conversations_favs    ON conversations(user_id, is_favorite) WHERE is_favorite = TRUE;
CREATE INDEX idx_conversations_domain  ON conversations(user_id, expert_domain) WHERE expert_domain IS NOT NULL;
CREATE INDEX idx_conversations_project ON conversations(project_id) WHERE project_id IS NOT NULL;

-- ✅ NOUVEAU : index GIN full-text pour la recherche dans l'historique
CREATE INDEX idx_conversations_fts ON conversations
    USING gin(to_tsvector('french', COALESCE(title, '')));
-- Usage : WHERE to_tsvector('french', title) @@ plainto_tsquery('french', :query)
```

### Table `messages`
```sql
CREATE TABLE messages (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role                VARCHAR(20) NOT NULL,       -- 'user' | 'assistant' | 'system'
    content             TEXT NOT NULL,
    content_type        VARCHAR(30) DEFAULT 'text', -- 'text' | 'image_generation'
                                                   -- | 'video_generation' | 'audio_generation'
                                                   -- | 'file_analysis' | 'voice_transcription'
                                                   -- (synchronisé avec HomeMessageType Flutter)
    model               VARCHAR(50),
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    latency_ms          INTEGER,
    liked               BOOLEAN,                   -- true=like | false=dislike | null=rien
    media_urls          JSONB,                     -- URLs des médias générés
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_messages_conversation ON messages(conversation_id, created_at ASC);
```

### Table `projects`
```sql
CREATE TABLE projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            VARCHAR(100) NOT NULL,
    icon            VARCHAR(50) DEFAULT 'folder',     -- clé icône catalogue Flutter
    color           VARCHAR(20) DEFAULT '#2E9BF0',    -- hex
    instructions    TEXT,                              -- prompt système injecté dans toutes les convs
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at      TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_projects_user ON projects(user_id, deleted_at);
```

### Table `project_files`
```sql
CREATE TABLE project_files (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    file_type       VARCHAR(50),              -- 'pdf' | 'docx' | 'image' | 'audio' | 'video'
    mime_type       VARCHAR(100),
    size_bytes      BIGINT,
    storage_key     VARCHAR(500) NOT NULL,    -- Clé S3/MinIO
    is_indexed      BOOLEAN DEFAULT FALSE,   -- Indexé dans pgvector
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### Table `scheduled_tasks` (Prompt Scheduler)
```sql
CREATE TABLE scheduled_tasks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title               VARCHAR(255) NOT NULL,
    prompt              TEXT NOT NULL,
    frequency           VARCHAR(20) NOT NULL, -- 'once'|'daily'|'weekly'|'monthly'|'yearly'
    scheduled_at        TIMESTAMP WITH TIME ZONE,  -- Pour 'once'
    cron_expression     VARCHAR(50),               -- Pour récurrents
    timezone            VARCHAR(50) DEFAULT 'Africa/Douala',
    next_run_at         TIMESTAMP WITH TIME ZONE,
    last_run_at         TIMESTAMP WITH TIME ZONE,
    status              VARCHAR(20) DEFAULT 'scheduled',
                        -- 'scheduled'|'running'|'completed'|'failed'|'cancelled'
    notify_push         BOOLEAN DEFAULT TRUE,
    notify_email        BOOLEAN DEFAULT FALSE,
    expert_domain       VARCHAR(50),
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_tasks_due  ON scheduled_tasks(status, next_run_at);
CREATE INDEX idx_tasks_user ON scheduled_tasks(user_id);
```

### Table `task_results`
```sql
CREATE TABLE task_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    output          TEXT,
    model           VARCHAR(50),
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    status          VARCHAR(20),              -- 'success' | 'failed'
    error_message   TEXT,
    executed_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### Table `media_items` (Bibliothèque)
```sql
CREATE TABLE media_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id     UUID REFERENCES conversations(id) ON DELETE SET NULL,
    message_id          UUID REFERENCES messages(id),
    type                VARCHAR(20) NOT NULL, -- 'image'|'video'|'audio'|'document'
    title               VARCHAR(255),
    storage_key         VARCHAR(500) NOT NULL,
    thumbnail_key       VARCHAR(500),
    mime_type           VARCHAR(100),
    size_bytes          BIGINT,
    prompt              TEXT,                 -- prompt de génération
    model               VARCHAR(50),
    metadata            JSONB,               -- durée, dimensions, etc.
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    deleted_at          TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_media_user ON media_items(user_id, type, created_at DESC);
```

### Table `voices`
```sql
CREATE TABLE voices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(100) NOT NULL,    -- "Aurora", "Memora", "Eron", "N'yanga"
    description     TEXT,
    sample_url      VARCHAR(500),             -- URL audio échantillon
    provider_ref    VARCHAR(255) NOT NULL,    -- référence fournisseur TTS
    is_active       BOOLEAN DEFAULT TRUE,
    sort_order      INTEGER DEFAULT 0,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
-- Jamais hardcodé côté frontend — chargé dynamiquement via GET /voice/list

CREATE INDEX idx_voices_active ON voices(is_active, sort_order);
```

### Table `notifications`
```sql
CREATE TABLE notifications (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        VARCHAR(30) NOT NULL,         -- 'planner_result'|'system'|'subscription'|'media_ready'
    title       VARCHAR(255) NOT NULL,
    body        TEXT,
    data        JSONB,                        -- {task_id, conversation_id, etc.}
    is_read     BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_notifications_user ON notifications(user_id, is_read, created_at DESC);
```

### Table `subscriptions`
```sql
CREATE TABLE subscriptions (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan                     VARCHAR(30) NOT NULL,   -- 'pro_monthly' | 'pro_yearly'
    provider                 VARCHAR(30) NOT NULL,   -- 'cinetpay'|'notchpay'|'orange_money'|'mtn'|'wave'|'airtel'|'card'
    provider_transaction_id  VARCHAR(255),
    phone_number             VARCHAR(30),
    amount                   INTEGER NOT NULL,        -- en FCFA (entier, pas de sous-unité)
    currency                 VARCHAR(10) DEFAULT 'XAF',
    status                   VARCHAR(20) DEFAULT 'pending',
                             -- 'active'|'cancelled'|'expired'|'pending'
    starts_at                TIMESTAMP WITH TIME ZONE,
    expires_at               TIMESTAMP WITH TIME ZONE,
    auto_renew               BOOLEAN DEFAULT TRUE,
    created_at               TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_subscriptions_user ON subscriptions(user_id, status);
```

### Table `payments`
```sql
CREATE TABLE payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id     UUID NOT NULL REFERENCES subscriptions(id),
    amount              INTEGER NOT NULL,           -- en FCFA
    currency            VARCHAR(10) DEFAULT 'XAF',
    status              VARCHAR(20),               -- 'pending'|'success'|'failed'|'refunded'
    provider_ref        VARCHAR(255),               -- référence transaction opérateur
    provider_data       JSONB,                      -- réponse brute du provider
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### Table `processed_webhooks` ✅ NOUVEAU
```sql
-- Garantit l'idempotence des webhooks paiement.
-- CinetPay/NotchPay peuvent renvoyer le même webhook plusieurs fois.
-- Avant de traiter un webhook, vérifier que provider_ref n'existe pas déjà.
CREATE TABLE processed_webhooks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_ref    VARCHAR(255) UNIQUE NOT NULL,   -- ID transaction opérateur
    provider        VARCHAR(30) NOT NULL,            -- 'cinetpay' | 'notchpay'
    processed_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_processed_webhooks_ref ON processed_webhooks(provider_ref);
-- Usage dans le service :
-- INSERT INTO processed_webhooks (provider_ref, provider)
-- VALUES (:ref, :provider)
-- ON CONFLICT (provider_ref) DO NOTHING RETURNING id
-- → Si RETURNING retourne NULL → webhook déjà traité → ignorer silencieusement
```

### Tables `usage_records` + `usage_daily`
```sql
CREATE TABLE usage_records (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    model           VARCHAR(50) NOT NULL,
    feature         VARCHAR(30) NOT NULL, -- 'chat'|'voice'|'vision'|'scheduler'|'image_gen'|'video_gen'
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_usd        NUMERIC(10,6),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_usage_user_date ON usage_records(user_id, created_at DESC);

-- Agrégat quotidien pour rate limiting rapide (évite COUNT(*) sur toute la table)
CREATE TABLE usage_daily (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date            DATE NOT NULL,
    feature         VARCHAR(30) NOT NULL,
    count           INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    total_cost_usd  NUMERIC(10,6) DEFAULT 0,
    UNIQUE(user_id, date, feature)
);

CREATE INDEX idx_usage_daily_lookup ON usage_daily(user_id, date);
```

### Table `memory_chunks` (Vector DB via pgvector)
```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE memory_chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- ✅ FK ajoutée
    source_type     VARCHAR(30) NOT NULL,  -- 'conversation' | 'file' | 'project'
    source_id       UUID NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(1536),          -- pgvector (OpenAI ada-002)
    metadata        JSONB,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX idx_memory_user      ON memory_chunks(user_id);
CREATE INDEX idx_memory_embedding ON memory_chunks USING hnsw (embedding vector_cosine_ops);
```

### Table `suggestions`
```sql
CREATE TABLE suggestions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    type        VARCHAR(30) DEFAULT 'suggestion', -- 'suggestion' | 'bug'
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

---

## 4. Couche IA — Patterns clés

### 4.1 LlmRouter — Orchestrateur de décision

```
─────────────────────────────────────────────────────
              REQUÊTE CHAT ENTRANTE
  { message, expert_domain?, files?, project? }
──────────────────────┬──────────────────────────────
                      │
                      ▼
          ┌───────────────────────┐
          │  Fichier audio ?      ├──YES──► Whisper (STT)
          └─────────┬─────────────┘
                    │ NO
                    ▼
          ┌───────────────────────┐
          │  Image / Vidéo ?      ├──YES──► Gemini Pro Vision
          └─────────┬─────────────┘
                    │ NO
                    ▼
          ┌───────────────────────┐
          │  expert_domain ?      ├──YES──► Qwen 2.5 (Finance/Science/Langues/Cuisine)
          │                       ├──YES──► GPT-4o (Informatique / Studio)
          └─────────┬─────────────┘
                    │ NO
                    ▼
          ┌───────────────────────┐
          │  Génération média ?   ├─IMAGE─► DALL-E 3
          │                       ├─VIDEO─► Gemini (vidéo)
          │                       ├─AUDIO─► TTS provider
          └─────────┬─────────────┘
                    │ TEXT
                    ▼
          ┌───────────────────────┐
          │  Mémoire IA ?         ├──YES──► pgvector search → inject contexte
          └─────────┬─────────────┘
                    │
                    ▼
          GPT-4o-mini (chat général — rapport qualité/coût optimal)
```

```python
# app/ai/router.py
class LlmRouter:
    def select(self, ctx: RequestContext) -> LlmProvider:
        if ctx.has_audio:
            return WhisperProvider()
        if ctx.has_image or ctx.feature == "vision":
            return GeminiProvider()
        if ctx.expert_domain in ["finance", "science", "language", "cooking",
                                  "engineering", "productivity"]:
            return QwenProvider()
        if ctx.expert_domain == "computer":
            return OpenAIProvider(model="gpt-4o")
        if ctx.feature == "image_generation":
            return OpenAIProvider(model="dall-e-3")
        if ctx.feature == "video_generation":
            return GeminiProvider(mode="video")
        return OpenAIProvider(model="gpt-4o-mini")

    async def select_with_fallback(
        self,
        ctx: RequestContext,
        messages: list[dict],
        system: str,
    ) -> TurnResult:
        """
        Exécute l'appel IA avec fallback automatique.
        Si le provider principal échoue, bascule vers le provider de secours.
        Maximum 2 tentatives (primary → fallback). Pas de retry infini.
        """
        primary = self.select(ctx)
        fallback = self._get_fallback(primary)

        for attempt, provider in enumerate([primary, fallback]):
            if provider is None:
                break
            try:
                return await provider.complete(messages=messages, system=system)
            except LlmProviderError as exc:
                if not exc.is_retryable or attempt == 1:
                    raise
                log.warning("llm.fallback", from_model=primary.model,
                            to_model=fallback.model, reason=str(exc))
                continue

        raise LlmUnavailableError("All providers failed")
```

### Matrice de fallback LLM

```
Provider principal        → Fallback
──────────────────────────────────────────────────────────
OpenAI  gpt-4o            → OpenAI gpt-4o-mini
OpenAI  gpt-4o-mini       → Qwen qwen-turbo
OpenAI  dall-e-3          → Gemini (image generation)  ← si disponible, sinon erreur
Gemini  Pro Vision        → OpenAI gpt-4o-vision
Qwen    qwen-2.5          → OpenAI gpt-4o-mini
Whisper (STT)             → Aucun (service unique — erreur remontée à l'utilisateur)
TTS                       → Aucun (service unique — erreur remontée à l'utilisateur)
──────────────────────────────────────────────────────────
Règle : Whisper et TTS n'ont pas de fallback — ce sont des services audio
spécialisés sans équivalent interchangeable. L'erreur est remontée
proprement au frontend avec un message utilisateur.
```

```python
# app/ai/router.py (suite)
# Erreurs considérées "retryable" → déclenche le fallback
RETRYABLE_STATUS_CODES = {
    429,   # Rate limit opérateur IA (quota dépassé)
    500,   # Erreur interne provider
    502,   # Bad gateway (provider down)
    503,   # Service unavailable
    504,   # Gateway timeout
}
# Timeout réseau → également retryable (asyncio.TimeoutError)

def _get_fallback(self, primary: LlmProvider) -> LlmProvider | None:
    fallback_map = {
        ("openai",  "gpt-4o"):       OpenAIProvider(model="gpt-4o-mini"),
        ("openai",  "gpt-4o-mini"):  QwenProvider(model="qwen-turbo"),
        ("openai",  "dall-e-3"):     GeminiProvider(mode="image"),
        ("gemini",  "pro-vision"):   OpenAIProvider(model="gpt-4o"),
        ("qwen",    "qwen-2.5"):     OpenAIProvider(model="gpt-4o-mini"),
        ("whisper", "whisper-1"):    None,   # pas de fallback audio
        ("tts",     "tts-1"):        None,   # pas de fallback audio
    }
    return fallback_map.get((primary.provider_name, primary.model))
```

### 4.2 QueryEngine — Cycle de vie d'un turn

```python
# app/ai/engine/query_engine.py
class QueryEngine:
    async def run(self, request: ChatRequest, user: User) -> TurnResult:
        session = await self.session_store.get_or_create(request.session_id)
        system_prompt = self.context_builder.build(
            expert_domain=request.expert_domain,
            project_instructions=request.project_instructions,
            user_name=user.username,
        )
        ctx = RequestContext.from_request(request)
        messages = session.messages + [{"role": "user", "content": request.prompt}]

        # select_with_fallback() — fallback automatique si le provider principal échoue
        # primary → fallback (max 2 tentatives, jamais de retry infini)
        result = await self.router.select_with_fallback(
            ctx=ctx,
            messages=messages,
            system=system_prompt,
        )

        await self.session_store.append(session.id, request.prompt, result.output)
        await self.cost_tracker.record(user.id, result.model_used, result.usage, "chat")
        return TurnResult(
            session_id=session.id,
            output=result.output,
            model_used=result.model_used,
        )
```

### 4.3 SSE Streaming + Annulation ✅ COMPLÉTÉ

```python
# app/ai/engine/stream_handler.py
async def stream_to_sse(
    provider: LlmProvider,
    messages: list[dict],
    system: str,
    conversation_id: str,
) -> AsyncGenerator[str, None]:
    """
    Stream SSE avec annulation via Redis pub/sub.
    POST /chat/stop → redis.setex("cancel:{conversation_id}", 60, "1")
    Le générateur vérifie la clé à chaque chunk et s'arrête proprement.
    Heartbeat toutes les 15s pour maintenir la connexion sur réseau 2G/3G.
    """
    cancel_key = f"cancel:{conversation_id}"
    last_heartbeat = time.monotonic()

    async for chunk in provider.stream(messages, system):
        # Vérification annulation à chaque chunk
        if await redis.exists(cancel_key):
            await redis.delete(cancel_key)
            yield f"data: {json.dumps({'done': True, 'cancelled': True})}\n\n"
            return

        # Heartbeat toutes les 15s (maintient la connexion sur 2G/3G)
        now = time.monotonic()
        if now - last_heartbeat >= 15:
            yield ":keepalive\n\n"
            last_heartbeat = now

        content = chunk.choices[0].delta.content or ""
        if content:
            yield f"data: {json.dumps({'content': content, 'done': False})}\n\n"

    yield f"data: {json.dumps({'done': True})}\n\n"


# Router FastAPI
@router.post("/chat/stream")
async def stream_chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return StreamingResponse(
        chat_service.stream(request, current_user, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/stop")
async def stop_chat(
    body: ChatStopRequest,
    current_user: User = Depends(get_current_user),
) -> NexyaResponse:
    """
    Publie un signal d'annulation dans Redis.
    Le générateur SSE actif détecte la clé et s'arrête proprement au prochain chunk.
    TTL 60s : nettoyage automatique si le stream est déjà terminé.
    """
    await redis.setex(f"cancel:{body.conversation_id}", 60, "1")
    return NexyaResponse(success=True)
```

### 4.4 Context Builder — Prompts Experts

```python
# app/ai/context_builder.py
EXPERT_SYSTEM_PROMPTS = {
    "computer":     "Tu es NEXYA, expert en informatique et développement. Tu maîtrises "
                    "algorithmes, architectures, tous les langages, cybersécurité, IA/ML. "
                    "Tu réponds avec des exemples de code concrets.",
    "science":      "Tu es NEXYA, expert en sciences et mathématiques...",
    "finance":      "Tu es NEXYA, expert en finance, investissement et business...",
    "language":     "Tu es NEXYA, expert en langues, traduction et cultures africaines...",
    "cooking":      "Tu es NEXYA, expert culinaire — cuisines africaines et du monde...",
    "engineering":  "Tu es NEXYA, expert en ingénierie et sciences appliquées...",
    "productivity": "Tu es NEXYA, expert en productivité, organisation et vie quotidienne...",
    "studio":       "Tu es NEXYA, expert créatif — design, musique, écriture, arts...",
}

DEFAULT_SYSTEM_PROMPT = """Tu es NEXYA, un assistant IA intelligent et bienveillant.
Tu aides les utilisateurs d'Afrique francophone et du monde entier.
Réponds toujours dans la langue de l'utilisateur. Sois concis et précis."""

def build_system_prompt(
    expert_domain: str | None,
    project_instructions: str | None,
    user_name: str | None,
) -> str:
    base = EXPERT_SYSTEM_PROMPTS.get(expert_domain or "", DEFAULT_SYSTEM_PROMPT)
    if user_name:
        base = f"L'utilisateur s'appelle {user_name}.\n\n" + base
    if project_instructions:
        base += f"\n\n## Instructions du projet\n{project_instructions}"
    return base
```

### 4.5 CostTracker — Suivi coûts + quotas

```python
# app/ai/cost_tracker.py
MODEL_COSTS_PER_1K = {
    "gpt-4o":       {"input": 0.0050, "output": 0.0150},
    "gpt-4o-mini":  {"input": 0.0002, "output": 0.0006},
    "gemini-pro":   {"input": 0.0005, "output": 0.0015},
    "qwen-turbo":   {"input": 0.0001, "output": 0.0002},
    "whisper-1":    {"per_minute": 0.006},
    "dall-e-3":     {"per_image": 0.040},
}

class CostTracker:
    async def record(self, user_id: str, model: str, usage: UsageSummary, feature: str):
        cost = self._calculate(model, usage)
        await db.insert(UsageRecord(...))
        # UPSERT agrégat quotidien (O(1) pour rate limiting)
        await db.execute("""
            INSERT INTO usage_daily (user_id, date, feature, count, total_tokens, total_cost_usd)
            VALUES (:user_id, CURRENT_DATE, :feature, 1, :tokens, :cost)
            ON CONFLICT (user_id, date, feature)
            DO UPDATE SET count = usage_daily.count + 1,
                          total_tokens = usage_daily.total_tokens + :tokens,
                          total_cost_usd = usage_daily.total_cost_usd + :cost
        """, ...)
        await self._check_daily_quota(user_id, feature)
```

### 4.6 SessionStore — Redis TTL + flush PostgreSQL

```python
# app/ai/engine/session_store.py
class SessionStore:
    """Sessions Redis (TTL 24h) → flushed vers PostgreSQL à la fermeture"""

    async def get_or_create(self, session_id: str | None) -> StoredSession:
        if session_id:
            cached = await redis.get(f"session:{session_id}")
            if cached:
                return StoredSession(**json.loads(cached))
        return StoredSession(session_id=uuid4().hex, messages=[])

    async def append(self, session_id: str, user_msg: str, assistant_msg: str):
        session = await self.get_or_create(session_id)
        session.messages.extend([
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ])
        await redis.setex(f"session:{session_id}", 86400, json.dumps(asdict(session)))
```

---

## 5. API Reference — Contrat avec le frontend Flutter

> Correspond exactement à la section 12 du CLAUDE.md frontend.
> Base URL : `https://api.nexya.ai/v1`
> Auth : `Authorization: Bearer <access_token>` sur toutes les routes protégées.

### AUTH
```
POST   /auth/register       { email, password, username }        → { access_token, refresh_token, user }
POST   /auth/login          { email, password }                  → { access_token, refresh_token, user }
POST   /auth/refresh        { refresh_token }                    → { access_token, refresh_token }
POST   /auth/logout         Header: Bearer token                 → révocation refresh token
GET    /user/profile                                             → UserProfile
PUT    /user/profile        { username?, avatar_url?, bio?, locale?, voice_id? }
PUT    /user/password       { current_password, new_password }
DELETE /user/account                                             → suppression définitive (RGPD)
POST   /user/device-token   { token, platform }                  → enregistrement token FCM
DELETE /user/device-token   { token }                            → désactivation token FCM
```

### CHAT
```
POST   /chat                ChatRequest                          → ChatResponse
POST   /chat/stream         ChatRequest                          → SSE Stream (event: chunk|done|error)
POST   /chat/stop           { conversation_id }                  → annule le stream (Redis pub/sub)
POST   /chat/{message_id}/feedback  { liked: bool|null }         → feedback like/dislike

ChatRequest: {
  prompt: str,
  session_id: str | None,        # None = nouvelle conversation
  conversation_id: str | None,
  expert_domain: str | None,     # None = chat général (Geek-1)
  project_id: str | None,
  media_urls: list[str] | None,  # images pour vision
  is_ephemeral: bool,
}
```

### HISTORIQUE
```
GET    /history             ?page&limit&domain?&search?&favorites?&archived?
                            → PaginatedResponse[Conversation]
                            (search utilise l'index GIN full-text)
PATCH  /history/{id}        { title?, is_favorite?, is_archived?, is_deleted?, project_id? }
DELETE /history/{id}        → suppression définitive
GET    /history/{id}/messages  ?cursor&limit   → PaginatedResponse[Message]
                               (cursor-based — évite OFFSET sur grandes tables)
```

### PROJETS
```
POST   /projects            { title, icon, color }               → Project
GET    /projects            ?page&limit                          → PaginatedResponse[Project]
GET    /projects/{id}                                            → Project + stats
PUT    /projects/{id}       { title?, icon?, color?, instructions? }
DELETE /projects/{id}                                            → soft delete
GET    /projects/{id}/conversations                              → PaginatedResponse[Conversation]
POST   /projects/{id}/files    multipart/form-data               → ProjectFile
GET    /projects/{id}/files                                      → list[ProjectFile]
DELETE /projects/{id}/files/{file_id}
```

### VOIX
```
GET    /voice/list                                               → list[Voice]  (dynamique)
POST   /voice/transcribe    multipart/form-data (audio)         → { text: str }
POST   /voice/speak         { text, voice_id }                  → audio/mpeg stream
```

### PLANIFICATEUR
```
POST   /tasks               { title, prompt, frequency, scheduled_at?, notify_push?, notify_email? }
GET    /tasks               ?page&limit&status?                  → PaginatedResponse[ScheduledTask]
GET    /tasks/{id}                                               → ScheduledTask + derniers résultats
PUT    /tasks/{id}
DELETE /tasks/{id}
GET    /tasks/{id}/results  ?page&limit                          → PaginatedResponse[TaskResult]
```

### FICHIERS & VISION
```
POST   /file/upload         multipart/form-data                 → { file_id, storage_key, url }
POST   /vision/analyze      { image_url, prompt? }              → { analysis: str }
```

### MÉMOIRE IA
```
POST   /memory/index        { source_type, source_id }          → { indexed: true }
POST   /memory/search       { query, limit? }                   → [{ content, score, source }]
```

### BIBLIOTHÈQUE
```
GET    /library             ?type?&page&limit                    → PaginatedResponse[MediaItem]
GET    /library/{id}                                             → MediaItem
DELETE /library/{id}                                             → soft delete
```

### MODÈLES
```
GET    /models                                                   → [{ id, name, provider, capabilities, status }]
```

### NOTIFICATIONS
```
GET    /notifications       ?unread_only?&type?&page&limit       → PaginatedResponse[Notification]
POST   /notifications/read  { ids: list[str] } ou { all: true }
DELETE /notifications/{id}
```

### ABONNEMENTS
```
GET    /subscriptions/status                                     → { plan, expires_at, usage_today }
POST   /subscriptions/checkout  { plan, provider, phone_number? } → { payment_url, transaction_id }
POST   /subscriptions/webhook/{provider}                         → Webhook CinetPay/NotchPay (HMAC + idempotence)
POST   /subscriptions/cancel
```

### SUGGESTIONS
```
POST   /suggestions         { content, type }                   → 201 Created
```

---

## 6. Format de réponse standard

```python
# app/shared/schemas.py
from typing import Generic, TypeVar
T = TypeVar("T")

class NexyaResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    limit: int
    has_next: bool
```

---

## 7. Prompt Scheduler — Architecture arq

```python
# workers/worker.py
from arq import cron
from arq.connections import RedisSettings

async def execute_scheduled_task(ctx, task_id: str):
    db = ctx["db"]
    task = await db.get(ScheduledTask, task_id)
    task.status = "running"
    await db.commit()

    try:
        task_ctx = RequestContext(expert_domain=task.expert_domain)
        messages  = [{"role": "user", "content": task.prompt}]
        system    = context_builder.build(task.expert_domain)

        # select_with_fallback() — même résilience que le chat principal
        result = await llm_router.select_with_fallback(
            ctx=task_ctx,
            messages=messages,
            system=system,
        )

        await db.insert(TaskResult(
            task_id=task_id,
            output=result.output,
            model=result.model_used,
            status="success",
        ))
        await cost_tracker.record(task.user_id, result.model_used, result.usage, "scheduler")
        if task.notify_push:
            await notification_service.send_push(task.user_id, f'Tâche "{task.title}" terminée')
        await schedule_next_run(task)

    except Exception as exc:
        await db.insert(TaskResult(task_id=task_id, status="failed", error_message=str(exc)))
        raise  # arq gère le retry automatiquement


async def dispatch_due_tasks(ctx):
    due_tasks = await ctx["db"].execute(
        select(ScheduledTask).where(
            ScheduledTask.next_run_at <= datetime.utcnow(),
            ScheduledTask.status.in_(["scheduled", "completed"]),
        )
    )
    for task in due_tasks.scalars():
        await ctx["redis"].enqueue_job("execute_scheduled_task", str(task.id))


class WorkerSettings:
    functions   = [execute_scheduled_task]
    cron_jobs   = [cron(dispatch_due_tasks, minute=None)]
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs    = 10
    job_timeout = 120
```

---

## 8. Paiements Afrique — Architecture extensible

```python
# app/features/subscriptions/payments/base.py
class PaymentProvider(ABC):
    @abstractmethod
    async def initiate(self, amount: int, currency: str, phone: str, description: str) -> PaymentIntent:
        """Initie un paiement. amount en FCFA (entier). Retourne URL de paiement ou OTP."""

    @abstractmethod
    async def verify_webhook(self, payload: dict, signature: str) -> WebhookEvent:
        """Vérifie et parse un webhook entrant. Validation HMAC obligatoire."""


# MVP — Agrégateurs (une seule intégration = tous les opérateurs)
class CinetPayProvider(PaymentProvider): ...
class NotchPayProvider(PaymentProvider): ...

# Extension directe si besoin
class OrangeMoneyProvider(PaymentProvider): ...
class MTNProvider(PaymentProvider): ...


# Service webhook — idempotence via processed_webhooks ✅
class SubscriptionService:
    async def handle_webhook(self, provider_ref: str, provider: str, payload: dict):
        # Tentative d'insertion — UNIQUE sur provider_ref
        result = await db.execute("""
            INSERT INTO processed_webhooks (provider_ref, provider)
            VALUES (:ref, :provider)
            ON CONFLICT (provider_ref) DO NOTHING
            RETURNING id
        """, {"ref": provider_ref, "provider": provider})

        if result.fetchone() is None:
            # Webhook déjà traité — ignorer silencieusement
            log.info("webhook.duplicate", provider_ref=provider_ref)
            return

        # Traitement normal du webhook (activer abonnement, etc.)
        await self._process_payment_success(payload)
```

---

## 9. Sécurité — Non négociable

### JWT
- Access token : TTL **15 minutes** — algorithme **RS256**
- Refresh token : TTL **30 jours** — stocké hashé en DB + Redis blacklist
- Rotation automatique du refresh token à chaque usage
- Token révoqué → blacklist Redis instantanée

### Rate Limiting — Deux couches ✅

```python
# app/core/security/rate_limiter.py

# ── Couche 1 : par USER (authentifié) — plan FREE vs PRO ────────────────────
FREE_PLAN_LIMITS = {
    "chat_requests_per_day":    50,
    "voice_minutes_per_day":    5,
    "vision_requests_per_day":  10,
    "image_gen_per_day":        3,
    "video_gen_per_day":        1,
    "scheduler_tasks_max":      3,
}

PRO_PLAN_LIMITS = {
    "chat_requests_per_day":    1000,
    "voice_minutes_per_day":    120,
    "vision_requests_per_day":  200,
    "image_gen_per_day":        30,
    "video_gen_per_day":        10,
    "scheduler_tasks_max":      50,
}


# ── Couche 2 : par IP — endpoints auth non authentifiés ✅ NOUVEAU ───────────
# Protège contre les attaques brute-force AVANT authentification
# (avant auth, on n'a pas encore de user_id — le rate limit par user ne s'applique pas)
AUTH_IP_LIMITS = {
    "/auth/login":    {"requests": 10, "window_seconds": 60},   # 10 tentatives/min par IP
    "/auth/register": {"requests": 5,  "window_seconds": 60},   # 5 inscriptions/min par IP
    "/auth/refresh":  {"requests": 20, "window_seconds": 60},   # 20 refresh/min par IP
}

async def check_ip_rate_limit(request: Request):
    """
    Dépendance FastAPI injectée sur les endpoints auth.
    Utilise Redis sliding window par IP.
    Retourne 429 Too Many Requests si la limite est dépassée.
    """
    client_ip = request.client.host
    path = request.url.path

    if path not in AUTH_IP_LIMITS:
        return

    limit = AUTH_IP_LIMITS[path]
    key = f"ratelimit:ip:{client_ip}:{path}"

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, limit["window_seconds"])

    if count > limit["requests"]:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please wait before trying again.",
            headers={"Retry-After": str(limit["window_seconds"])},
        )

# Injection dans les routers auth :
# @router.post("/login")
# async def login(request: LoginRequest, _=Depends(check_ip_rate_limit)):
#     ...
```

### Sécurité globale
| Mesure | Implémentation |
|---|---|
| Passwords | bcrypt cost=12 |
| Inputs | Sanitizer avant tout appel IA — 10 000 chars max, strip prompt injections |
| Upload | Vérification MIME type + taille max (20MB images, 100MB vidéo) |
| SQL injection | SQLAlchemy parameterized queries |
| XSS | Sanitize HTML dans les réponses IA avant stockage |
| CORS | Whitelist strict (`app.nexya.ai`, Flutter app) |
| Webhook paiement | Validation signature HMAC + idempotence `processed_webhooks` |
| Logs | Jamais de tokens/passwords — redaction structlog |
| MFA | TOTP (Google Authenticator / Authy) — optionnel, géré via `mfa_secret` |

---

## 10. Performance — Optimisation réseau Afrique

### Timeouts agressifs (alignés sur `retry_interceptor.dart` Flutter)
```python
LLM_TIMEOUT    = 30    # secondes — timeout appel LLM
STREAM_TIMEOUT = 120   # secondes — timeout total stream SSE
UPLOAD_TIMEOUT = 60    # secondes — timeout upload fichier
```

### Compression
- **gzip/brotli** sur toutes les réponses JSON (Nginx)
- **HTTP/2** — multiplex les requêtes SSE sans ouvrir plusieurs connexions TCP

### Caching Redis
```python
@cached(redis, ttl=3600, key="voice:list")
async def get_voice_list(): ...

@cached(redis, ttl=300, key="user:{user_id}:profile")
async def get_user_profile(user_id): ...
```

### Pagination obligatoire
- Toutes les listes : `page` + `limit` (max 50 items)
- Messages d'une conversation : **cursor-based** (évite OFFSET sur grandes tables)

### Contraintes réseau Afrique
| Contrainte | Solution |
|---|---|
| Latence 200-500ms | Cloudflare CDN + edge caching + gzip |
| 2G/3G | Payloads compressés, pagination 20 items, thumbnails < 50KB, images WebP |
| Déconnexions fréquentes | SSE + `Last-Event-ID` pour reprise, idempotency keys sur POST |
| Coût data mobile | Pas de prefetch agressif, lazy loading côté API |
| Connexion SSE instable | Heartbeat `:keepalive\n\n` toutes les **15 secondes** |

---

## 11. Docker Compose (MVP Prod)

```yaml
version: "3.9"

services:
  app:
    build: .
    ports: ["8000:8000"]
    environment:
      - DATABASE_URL=postgresql+asyncpg://nexya:${DB_PASSWORD}@db:5432/nexya
      - REDIS_URL=redis://redis:6379
      - S3_ENDPOINT=http://minio:9000
      - JWT_PRIVATE_KEY=${JWT_PRIVATE_KEY}
      - JWT_PUBLIC_KEY=${JWT_PUBLIC_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - QWEN_API_KEY=${QWEN_API_KEY}
      - CINETPAY_API_KEY=${CINETPAY_API_KEY}
      - FCM_SERVER_KEY=${FCM_SERVER_KEY}
    depends_on: [db, redis, minio]
    restart: unless-stopped

  arq_worker:
    build: .
    command: python -m arq workers.worker.WorkerSettings
    environment:
      - DATABASE_URL=postgresql+asyncpg://nexya:${DB_PASSWORD}@db:5432/nexya
      - REDIS_URL=redis://redis:6379
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - QWEN_API_KEY=${QWEN_API_KEY}
      - FCM_SERVER_KEY=${FCM_SERVER_KEY}
    depends_on: [db, redis]
    restart: unless-stopped

  db:
    image: pgvector/pgvector:pg16
    volumes: ["pgdata:/var/lib/postgresql/data"]
    environment:
      POSTGRES_DB: nexya
      POSTGRES_USER: nexya
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    volumes: ["redisdata:/data"]
    restart: unless-stopped

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    volumes: ["miniodata:/data"]
    environment:
      MINIO_ROOT_USER: ${MINIO_USER}
      MINIO_ROOT_PASSWORD: ${MINIO_PASSWORD}
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports: ["80:80", "443:443"]
    restart: unless-stopped

volumes:
  pgdata:
  redisdata:
  miniodata:
```

---

## 12. Variables d'environnement

```bash
# .env.example — jamais de valeurs réelles ici

# Database
DATABASE_URL=postgresql+asyncpg://nexya:PASSWORD@localhost:5432/nexya

# Redis
REDIS_URL=redis://localhost:6379

# JWT RS256 (générer avec openssl)
# openssl genrsa -out private.pem 2048
# openssl rsa -in private.pem -pubout -out public.pem
JWT_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n..."
JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n..."

# IA
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AI...
QWEN_API_KEY=...

# Storage
S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_BUCKET_NAME=nexya-media

# Paiements
CINETPAY_API_KEY=...
CINETPAY_SITE_ID=...
NOTCHPAY_PUBLIC_KEY=...

# Notifications
FCM_SERVER_KEY=...

# App
ENV=development   # development | staging | production
DEBUG=true
ALLOWED_ORIGINS=http://localhost:3000,https://app.nexya.ai
```

---

## 13. Déploiement

### Hébergement recommandé
| Option | Usage | Prix |
|---|---|---|
| **Hetzner Cloud** (Helsinki/Nuremberg) | Prod backend + DB | ~20€/mois pour commencer |
| **Cloudflare** (gratuit) | CDN + DDoS + SSL | Gratuit |
| **Cloudflare R2** | Stockage médias (alternative MinIO) | Gratuit jusqu'à 10GB |

### Latence depuis l'Afrique
- Hetzner Helsinki → Cameroun : **~80-120ms**
- HTTP/2 + Cloudflare CDN réduit la latence perçue
- SSE : connexion persistante = pas de handshake TCP à chaque chunk

### Stratégie de scaling
| Phase | Users | Infra |
|---|---|---|
| MVP | 0 → 10k | 1 VPS (8 cores, 32GB RAM) + Docker Compose |
| Croissance | 10k → 100k | 2-3 VPS, load balancer Nginx, Redis Sentinel, PG replicas |
| Scale | 100k → 950k | Kubernetes (k3s), PG cluster, Redis Cluster, S3 dédié |

---

## 14. Ordre d'implémentation recommandé

```
Phase 1 — Foundation (semaine 1-2)
  ├── Setup projet FastAPI + Docker + PostgreSQL + Redis
  ├── Migrations Alembic (users, refresh_tokens, device_tokens, conversations, messages)
  ├── Auth JWT complet (register, login, refresh, logout, guards RS256)
  ├── Rate limiting IP sur /auth/login (anti brute-force)
  └── Tests d'intégration auth

Phase 2 — Chat Core (semaine 3-4)
  ├── LlmRouter + providers (OpenAI en premier)
  ├── QueryEngine + SessionStore
  ├── POST /chat (non-stream d'abord)
  ├── POST /chat/stream (SSE) + POST /chat/stop (annulation Redis)
  ├── CostTracker + usage_daily
  └── Tests streaming + cancellation

Phase 3 — Features IA (semaine 5-6)
  ├── Modes Experts (context_builder, tous les domaines)
  ├── Voice (Whisper STT + TTS)
  ├── Vision (Gemini)
  ├── File upload (S3)
  └── Memory IA (pgvector + embeddings)

Phase 4 — Features App (semaine 7-8)
  ├── Projects CRUD + fichiers
  ├── History + Favorites (search GIN full-text)
  ├── Library (médias générés)
  ├── Notifications (FCM via device_tokens)
  └── Prompt Scheduler (arq)

Phase 5 — Monétisation (semaine 9-10)
  ├── Subscriptions model
  ├── CinetPay integration (agrégateur MVP)
  ├── Webhook validation HMAC + idempotence processed_webhooks
  ├── Rate limiting par plan (usage_daily)
  └── NotchPay (alternative)

Phase 6 — Production
  ├── Optimisations performance (caching Redis, pagination)
  ├── Monitoring (Prometheus + Grafana + Sentry)
  ├── Load testing (locust ou k6)
  └── Deploy Hetzner + Cloudflare + Nginx HTTP/2
```

---

## 15. Conventions de code

### Nommage Python
- Fichiers : `snake_case.py`
- Classes : `PascalCase`
- URLs FastAPI : `kebab-case` pour les segments de route
- Schemas Pydantic : suffixe `Request` / `Response` / `Create` / `Update`
- Dépendances FastAPI : préfixe `get_` (ex: `get_current_user`, `get_db`)

### Structure d'un module FastAPI
```python
# Un module = router.py + service.py + schemas.py + models.py (si ORM)
router = APIRouter(prefix="/chat", tags=["chat"])

@router.post("/stream")
async def stream_chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return await chat_service.stream(request, current_user, db)
```

### Règles absolues
- ❌ Jamais de clé API dans le code — uniquement via variables d'env
- ❌ Jamais de `print()` — utiliser `structlog` uniquement
- ❌ Jamais de requête SQL sans paramètres bindés (injection)
- ❌ Jamais charger une liste entière sans pagination
- ❌ Jamais de logique métier dans le router — uniquement dans le service
- ❌ Jamais modifier le schéma DB sans migration Alembic
- ❌ Tout appel async enveloppé dans un `try/except`
- ❌ Erreurs typées (NexYaException) — jamais de raise Exception générique
- ✅ Tests unitaires sur LlmRouter, CostTracker, ContextBuilder (pas de vrai appel IA)
- ✅ Tests d'intégration avec base de test isolée (conftest.py)

---

## 16. Observabilité — Production-Grade

### Architecture du bloc

```
Requête entrante
      │
      ▼
  FastAPI middleware
      │
  ────┴───────────────────────────────
  │   OpenTelemetry Tracer            │  → trace_id unique par requête
  │   (tracing.py)                    │  → spans : API → LLM → DB → Redis
  ────┬───────────────────────────────
      │
  ────┴───────────────────────────────
  │   structlog                       │  → log JSON avec trace_id corrélé
  │   (logging.py)                    │  → jamais de print() en production
  ────┬───────────────────────────────
      │
  ────┴───────────────────────────────
  │   Prometheus Metrics              │  → /metrics exposé pour Grafana
  │   (metrics.py)                    │  → compteurs, histogrammes, jauges
  ────────────────────────────────────
```

### 16.1 Métriques Prometheus — Spécifiques NEXYA

```python
# app/core/observability/metrics.py
from prometheus_client import Counter, Histogram, Gauge

ai_requests_total = Counter(
    "nexya_ai_requests_total",
    "Nombre total d'appels IA",
    labelnames=["model", "feature", "expert_domain", "status"],
)

ai_request_duration_seconds = Histogram(
    "nexya_ai_request_duration_seconds",
    "Latence des appels IA en secondes",
    labelnames=["model", "feature"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

ai_tokens_total = Counter(
    "nexya_ai_tokens_total",
    "Tokens consommés",
    labelnames=["model", "direction"],  # "input" | "output"
)

ai_cost_usd_total = Counter(
    "nexya_ai_cost_usd_total",
    "Coût total des appels IA en USD",
    labelnames=["model", "feature"],
)

sse_active_connections = Gauge(
    "nexya_sse_active_connections",
    "Connexions SSE actives en ce moment",
)

rate_limit_hits_total = Counter(
    "nexya_rate_limit_hits_total",
    "Requêtes bloquées par le rate limiter",
    labelnames=["plan", "feature"],  # plan: "free"|"pro"|"ip"
)
```

### 16.2 Dashboards Grafana — Ce qu'on surveille

| Dashboard | Métriques clés |
|---|---|
| **Vue IA** | Latence p50/p95/p99 par modèle, tokens/heure, coût USD/jour, taux d'erreurs LLM |
| **Vue Chat** | Connexions SSE actives, durée moyenne stream, req/s sur /chat/stream |
| **Vue Queue** | Jobs arq en attente, durée scheduled tasks, taux d'échec planificateur |
| **Vue HTTP** | Req/s par endpoint, p95 latence, taux erreurs 4xx/5xx |
| **Vue Business** | Users actifs/jour, req par plan (FREE vs PRO), rate limit hits par feature |
| **Vue Coûts** | Coût OpenAI/Gemini/Qwen par jour, coût par user, projection mensuelle |

### 16.3 Packages à ajouter dans pyproject.toml

```toml
# Observabilité
opentelemetry-api = "^1.24"
opentelemetry-sdk = "^1.24"
opentelemetry-instrumentation-fastapi = "^0.45"
opentelemetry-instrumentation-sqlalchemy = "^0.45"
opentelemetry-instrumentation-redis = "^0.45"
opentelemetry-exporter-otlp-proto-grpc = "^1.24"
prometheus-client = "^0.20"
structlog = "^24.1"
```

---

## 17. Journal des modifications

| Date | Tâche | Corrections appliquées |
|---|---|---|
| 2026-04-01 | Architecture initiale backend NEXYA | Structure complète, DB schema, patterns IA |
| 2026-04-01 | Corrections v2 — JWT RS256, arq, tables complètes | `refresh_tokens`, endpoints manquants |
| 2026-04-03 | Ajout section 16 Observabilité | OpenTelemetry, Prometheus, structlog, Grafana |
| 2026-04-01 | Fusion v2.0 — Merge CLAUDE_BACKEND + BACKEND_ARCHITECTURE | CinetPay/NotchPay, Docker Compose prod, heartbeat SSE |
| 2026-04-03 | **v3.0 — 6 corrections audit** | (1) FK `memory_chunks.user_id REFERENCES users` ; (2) Table `device_tokens` pour FCM push Flutter ; (3) Rate limiting par IP sur `/auth/login` `/auth/register` `/auth/refresh` (anti brute-force) ; (4) Table `processed_webhooks` + logique idempotence webhooks paiement ; (5) Mécanisme annulation SSE via Redis `cancel:{conversation_id}` + endpoint `/chat/stop` documenté ; (6) Index GIN full-text `idx_conversations_fts` sur `conversations.title` |
| 2026-04-03 | **v3.1 — Fallback LLM** | Section 4.1 enrichie : `select_with_fallback()`, matrice complète (7 providers), codes HTTP retryables (429/500/502/503/504 + asyncio.TimeoutError), `_get_fallback()`, règle Whisper/TTS sans fallback documentée |
| 2026-04-03 | **v3.2 — Câblage fallback** | `QueryEngine.run()` branché sur `select_with_fallback()` (était `provider.complete()` direct). `execute_scheduled_task()` idem — le scheduler bénéficie du même mécanisme de résilience que le chat principal. `result.model_used` propagé dans `cost_tracker.record()` pour tracking correct du modèle effectivement utilisé (primary ou fallback). |
