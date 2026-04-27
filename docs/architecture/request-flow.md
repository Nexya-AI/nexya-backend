# Request Flow — NEXYA Backend

> **Executive summary (EN).** End-to-end request flow for the 4 most
> critical NEXYA endpoints: `POST /auth/login`, `POST /chat/stream`,
> `POST /files/upload`, `GET /rgpd/user/data-export`. Each flow shows
> middleware ordering, validation, business logic, persistence, and
> observability hooks. Sequence diagrams in Mermaid.

---

## 1. `POST /auth/login`

```mermaid
sequenceDiagram
    participant App as Flutter App
    participant CF as Cloudflare
    participant MW as Middlewares (Security/Trace/CORS)
    participant Router as auth_router
    participant Service as AuthService
    participant DB as PostgreSQL
    participant Redis as Redis (rate limit)

    App->>CF: POST /auth/login {email, password}
    CF->>MW: forward
    MW->>MW: NexyaSecurityHeadersMiddleware (pose CSP/HSTS prod)
    MW->>MW: TraceIdMiddleware (génère trace_id, bind structlog)
    MW->>MW: CORSMiddleware (validate origin)
    MW->>Router: dispatch
    Router->>Redis: check_ip_rate_limit (10/min/IP)
    alt Rate limit exceeded
        Redis-->>Router: 429 RATE_LIMIT_IP
        Router-->>App: NexyaResponse(success=false, code="RATE_LIMIT_IP")
    else OK
        Router->>Service: login(body, db, ip, ua, device_id)
        Service->>DB: SELECT user WHERE email
        Service->>Service: bcrypt.verify(password, hash)
        alt Invalid
            Service->>DB: INSERT auth_events (login_failed)
            Service-->>Router: AuthCredentialsInvalidException
            Router-->>App: 401 AUTH_CREDENTIALS_INVALID
        else Valid
            Service->>Service: create_access_token (RS256, TTL 15min)
            Service->>Service: create_refresh_token (TTL 30j, hash SHA-256)
            Service->>DB: INSERT refresh_tokens (token_hash, user_id, expires_at)
            Service->>DB: INSERT auth_events (login_success, ip, ua)
            Service-->>Router: TokenResponse
            Router-->>App: NexyaResponse(success=true, data=tokens)
        end
    end
```

**Observabilité** : `auth_events` (forensic), trace_id corrélé dans tous
les logs structlog, OTel span auto-instrumenté FastAPI.

---

## 2. `POST /chat/stream` (le cœur du produit)

```mermaid
sequenceDiagram
    participant App as Flutter App
    participant Router as chat_router
    participant Guards as get_current_user
    participant Budget as BudgetTracker
    participant Mod as ModerationService
    participant Rules as moderation_rules
    participant Memory as MemoryStore
    participant Token as TokenEstimator
    participant Cache as PromptCache
    participant LLM as LlmRouter + StreamHandler
    participant Provider as Gemini/OpenAI/...
    participant CT as CostTracker

    App->>Router: POST /chat/stream {message, expert_id, conversation_id}
    Router->>Guards: validate JWT → User
    Router->>Budget: check_and_consume_chat (50/jour Free, 1000 Pro)
    alt Budget exceeded
        Router-->>App: 429 RATE_LIMIT_EXCEEDED + reset_at
    end
    Router->>Mod: check_async (OpenAI omni-moderation, fail-open 3s)
    Router->>Rules: check_business_rules (regex prescription/legal)
    alt Rules rejected
        Router-->>App: 400 CONTENT_FILTERED + disclaimer
    end
    Router->>Memory: build_memory_context (D3 — top-K user facts)
    Router->>Token: enforce_prompt_token_cap (max 30k)
    alt Cap exceeded
        Router-->>App: 402 LLM_QUOTA_EXCEEDED
    end
    Router->>Cache: get(cache_key) — skip safety-critical, skip multi-turn
    alt Cache HIT (legacy stateless mode)
        Cache-->>Router: CachedCompletion
        Router-->>App: SSE replay (X-Cache: HIT)
    else MISS
        Router->>LLM: stream(StreamContext)
        loop For each fallback link
            LLM->>Provider: stream_chat with retry policy
            alt Retryable error
                LLM->>LLM: next link in chain
            end
        end
        Provider-->>LLM: ChatChunk stream
        LLM-->>Router: SSE events (chunk/keepalive 15s/done)
        Router-->>App: SSE stream
        Router->>CT: record_ai_call_background (fire-and-forget)
        CT->>CT: INSERT ai_calls + UPSERT usage_daily
        Router->>Cache: put(cache_key, completion) [if cacheable]
    end
```

**Annulation duale** : Redis key `chat:cancel:{session_id}` posée par
`POST /chat/stop` OU déconnexion HTTP détectée par
`Request.is_disconnected()`. Le `StreamHandler` check ces 2 voies
toutes les 1-2s pendant le stream.

**Heartbeat** : `: keepalive` envoyé toutes les 15s pour éviter la
coupure des proxies 2G/3G après inactivité TCP.

**Cost tracking fire-and-forget** : `asyncio.create_task` lance
`CostTracker.record_ai_call` en arrière-plan — le SSE ne bloque
JAMAIS sur l'écriture DB. Si elle crash, on log warning.

---

## 3. `POST /files/upload`

```mermaid
sequenceDiagram
    participant App as Flutter App
    participant Router as files_router
    participant Service as FileUploadService
    participant Detector as MimeDetector
    participant Scanner as VirusScanner
    participant Store as ObjectStore (MinIO)
    participant Extractor as TextExtractor
    participant DB as PostgreSQL
    participant Worker as arq Worker (RAG)

    App->>Router: POST /files/upload (multipart, 1MB PDF)
    Router->>Router: rate_limit user 20/h
    Router->>Service: upload(file, user, db)

    Service->>Service: 1. MIME whitelist check → 415 si KO
    Service->>Service: 2. Read streaming + cap 100MB → 413 si KO
    Service->>Service: 3. Compute SHA-256 incrémental
    Service->>Detector: detect_mime_type (magic-bytes 4KB) → 415 si mismatch
    Service->>DB: 5. Dédup SELECT WHERE (user_id, sha256) → return existing if hit
    Service->>Scanner: scan(data, filename) → 415 VIRUS_DETECTED si suspicious
    Service->>Store: upload_bytes (MinIO + sharding 2-char SHA)
    Service->>DB: INSERT uploaded_files (status='pending')
    Service->>Extractor: asyncio.to_thread (pypdf/python-docx)
    alt Extraction OK
        Service->>DB: UPDATE extraction_status='ok' + extracted_text
    else Extraction failed
        Service->>DB: UPDATE extraction_status='failed' (upload reste valid)
    end
    Service->>Worker: enqueue index_document_chunks (fail-silent)
    Service-->>Router: UploadedFileResponse
    Router-->>App: 201 + presigned URL TTL 30min

    Worker-->>DB: INSERT document_chunks (RAG indexation async)
```

**Anti-smuggling** : MIME annoncé vs détecté magic-bytes — si mismatch
(ex: client poste `.exe` avec `Content-Type: image/png`), 415
`FILE_CONTENT_MISMATCH` avant upload MinIO.

**Pipeline strict 10 étapes court-circuitantes** : un payload abusif
rejeté au step 1 (MIME hors whitelist) consomme ~0 ms vs upload puis
refus = ~100 MB disk + 200 ms CPU.

**Fail-safe extraction** : pypdf crash sur PDF corrompu ne casse PAS
l'upload. Status `failed` informatif, l'user garde son fichier.

---

## 4. `GET /rgpd/user/data-export`

```mermaid
sequenceDiagram
    participant User as User authenticated
    participant Router as rgpd_router
    participant Service as DataExportService
    participant DB as PostgreSQL
    participant Store as ObjectStore (MinIO)
    participant Audit as auth_events

    User->>Router: GET /rgpd/user/data-export
    Router->>Router: rate_limit_user 1/24h
    Router->>Service: build_export(user, db)

    par Parallel reads — 18 tables user-scope
        Service->>DB: SELECT users (sans password_hash)
        Service->>DB: SELECT consent_log (historique complet)
        Service->>DB: SELECT auth_events (IP anonymisée /24)
        Service->>DB: SELECT conversations + messages
        Service->>DB: SELECT projects + files
        Service->>DB: SELECT library_items
        Service->>DB: SELECT memories (sans embeddings — trop volumineux)
        Service->>DB: SELECT notifications + preferences
        Service->>DB: SELECT scheduled_tasks + results
        Service->>DB: SELECT uploaded_files + chunks (sans embeddings)
        Service->>DB: SELECT voice_transcriptions
        Service->>DB: SELECT vision_analyses
        Service->>DB: SELECT ai_calls (sans extra prompt content)
    end

    Service->>Store: presign_url (chaque blob, TTL 7j)
    Service->>Service: build ZIP en mémoire (BytesIO + zipfile.ZIP_DEFLATED)
    note right of Service: 23 fichiers JSON + README FR + manifest
    alt ZIP > 100 MB
        Service->>Service: flag truncated=true dans manifest
    end
    Service-->>Router: StreamingResponse application/zip
    Router->>Audit: log_auth_event(data_exported, user_id)
    Router-->>User: ZIP download (Content-Disposition: attachment)
```

**Conformité RGPD Article 20** : portabilité format structuré JSON
+ presigned URLs pour les blobs (alternative au base64 inline qui
ferait exploser le ZIP à 500 MB pour user lourd).

**Anti-leak** : 0 password_hash, 0 storage_key brut, IPs anonymisées
en /24 IPv4 ou /48 IPv6. Voir
[`docs/compliance/rgpd.md`](../compliance/rgpd.md).

---

## Patterns transverses

### Error handling global

Tous les `NexYaException` sont catchés par `nexya_exception_handler`
dans `app/core/errors/handlers.py` :
1. Log structlog avec trace_id
2. **Hook escalation Crisp** (Phase 18 / N4) si user Pro + payment/LLM
3. JSONResponse `NexyaResponse(success=false, code, message, data)`

### Audit trail
Toutes les actions sensibles (auth, RGPD, paiements) sont tracées dans
`auth_events` avec `event_type` + `user_id` + `ip` + `user_agent` +
`metadata_json`. FK `ON DELETE SET NULL` pour préserver post-purge user.

### Observabilité
Chaque request HTTP :
1. `TraceIdMiddleware` génère/propage `X-Request-ID`
2. `structlog.contextvars` bind `trace_id` → tous les logs corrélés
3. OTel auto-instrument FastAPI → span racine `http.server`
4. `OTLPSpanExporter` → collector externe (Jaeger/Tempo)
5. Prometheus métriques selon endpoint (`nexya_ai_*`, `nexya_arq_*`, etc.)

Voir [`observability.md`](observability.md) pour le détail.
