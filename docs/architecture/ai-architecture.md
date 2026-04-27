# AI Architecture — NEXYA Backend

> **Executive summary (EN).** AI Layer is the product core. `LlmRouter`
> resolves `expert_id` → `(provider, model, ExpertConfig)` triplet. 11
> domain experts with primary + fallback chains. 4 real chat providers
> (Gemini, OpenAI, Anthropic, Qwen) + OpenRouter aggregator + Mock
> usurping identity (mock-first pattern). Resilience: RetryPolicy +
> CircuitBreaker + budget tracker + prompt cache + token estimator +
> moderation rules. Observability: StreamMetrics + cost_tracker
> fire-and-forget + Prometheus metrics. SSE streaming with 15s
> heartbeat + dual cancellation (Redis key + HTTP disconnect).
> 8 SaaS integrations follow mock-first pattern: Brevo, hCaptcha, FCM,
> Vision, Voice, Embeddings, Crisp, MinIO.

---

## Vision en une phrase

**Le backend choisit toujours le modèle**, jamais le frontend. Le
client envoie un `expert_id` (« computer », « medicine »...), le
`LlmRouter` traduit en triplet concret `(ChatProvider, model_name,
ExpertConfig)` qui contient le `system_prompt` métier, la fallback
chain, la température et les disclaimers.

---

## LlmRouter

### Résolution

```python
router.resolve("medicine") → ChatResolution(
    provider=GeminiChatProvider,
    model="gemini-2.5-pro",
    config=EXPERT_REGISTRY["medicine"]  # frozen ExpertConfig
)
```

### `ExpertConfig` (frozen dataclass)

```python
@dataclass(frozen=True, slots=True)
class ExpertConfig:
    expert_id: str                      # contrat API stable Flutter
    display_name: str                   # UI label (peut évoluer)
    is_coming_soon: bool
    primary_provider: str               # "gemini", "openai", ...
    primary_model: str                  # "gemini-2.5-flash", ...
    fallback_chain: tuple[tuple[str, str], ...]
    system_prompt: str                  # FR multi-paragraphes
    temperature: float = 0.7
    max_tokens: int | None = None
    disclaimer: str | None = None       # médecin/avocat
    tier: str = "flash"                 # flash | pro | image
    tags: tuple[str, ...] = ()
    corpus_enabled: bool = False        # G1 RAG
    tools_allowed: bool = True          # F2.5 — False sur safety-critical
```

### 11 experts livrés

| `expert_id` | Tier | Modèle primaire | Fallback chain | Tools | Notes |
|---|---|---|---|---|---|
| `general` | flash | `gemini-2.5-flash` | Pro Gemini, OpenRouter Sonnet | ✅ | Conversation par défaut |
| `computer` | flash | `gemini-2.5-flash` | Pro Gemini | ✅ | Code, debug, archi |
| `science` | pro | `gemini-2.5-pro` | Flash, OpenRouter | ✅ | STEM rigueur |
| `finance` | flash | `gemini-2.5-flash` | Pro Gemini | ✅ | OHADA, FCFA |
| `language` | pro | `gemini-2.5-pro` | Flash | ✅ | Traduction, conjugaison |
| `cooking` | flash | `gemini-2.5-flash` | Pro Gemini | ✅ | Recettes africaines |
| `studio` | image | `imagen-3.0-generate-002` | — | ❌ | Image only, pas chat |
| `engineering` | pro | `gemini-2.5-pro` | Flash | ✅ | Coming soon |
| `productivity` | flash | `gemini-2.5-flash` | Pro, OpenRouter | ✅ | GTD, atomic habits |
| `medicine` | pro | `gemini-2.5-pro` | Flash | **❌** | Safety-critical, disclaimer médecin |
| `legal` | pro | `gemini-2.5-pro` | Flash | **❌** | Safety-critical, disclaimer avocat OHADA |

**Décision F2.5** : `tools_allowed=False` sur `medicine`/`legal` — un
expert médical ne doit pas créer une tâche Planner depuis une
consultation (side-effect DB silencieux).

**Permissif sur expert_id inconnu** : fallback `general` plutôt que
500. Si Flutter envoie un nouveau `expert_id` non encore déployé
backend, on sert le général. Aucun crash.

---

## Chat Providers (ABC + 5 réels + Mock)

### ABC `ChatProvider`

```python
class ChatProvider(ABC):
    name: str
    default_model: str
    supported_models: frozenset[str]
    capabilities: frozenset[ProviderCapability]
    max_context_tokens: int

    async def stream_chat(self, request) -> AsyncIterator[ChatChunk]:
        ...

    async def health_check(self) -> bool:
        ...
```

### Implementations

| Provider | SDK | Modes | Cas d'usage |
|---|---|---|---|
| `GeminiChatProvider` | `google-genai` async | Vertex AI ou AI Studio | Tous experts par défaut |
| `OpenAIChatProvider` | `openai>=1.55` | Chat + reasoning o1 | Fallback Gemini Pro |
| `AnthropicChatProvider` | `anthropic>=0.42` | `messages.stream()` ctx mgr | Fallback safety experts |
| `QwenChatProvider` | `openai SDK + base_url DashScope` | Compat OpenAI | Fallback chain |
| `OpenRouterChatProvider` | `openai SDK + openrouter.ai` | Agrégateur 5 modèles | Fallback non safety-critical |
| `MockChatProvider` | — | Identité usurpée (name/default_model du vrai) | Dev sans clé, tests, CI |

### Mock-first pattern

```python
def build_default_router() -> LlmRouter:
    real = _build_real_chat_providers()       # selon clés API présentes
    mocks = _build_mock_chat_providers()      # tous, identité usurpée

    chat_providers = {}
    for name in ("gemini", "openai", "anthropic", "qwen", "openrouter"):
        chat_providers[name] = real.get(name) or mocks[name]
```

Si Ivan met `OPENAI_API_KEY=` (vide), `MockChatProvider(name="openai",
default_model="gpt-4o", supported_models=...)` est utilisé. La
fallback chain `experts.py` continue de résoudre — pas de warning
`model_not_in_supported_set`.

---

## Résilience

### RetryPolicy

`app/ai/retry.py::RetryPolicy(max_attempts=3, base_delay=0.5,
max_delay=5, jitter_ratio=0.25)`. Honore `retry_after_seconds` si
fourni par le provider (rate limit). **Retry uniquement avant le 1er
chunk** — sinon le client voit du texte dupliqué côté SSE.
`asyncio.CancelledError` toujours propagé.

### CircuitBreaker

`app/ai/circuit_breaker.py` — par couple `(provider, model)`. État
`CLOSED → OPEN → HALF_OPEN`. Defaults : 5 échecs / 30s cooldown / 1
essai sondage. Erreurs `ProviderAuthError`/`ProviderContentFilteredError`
**n'ouvrent PAS** le circuit (bug NEXYA, pas panne provider).

### Fallback chain

`StreamHandler` itère `LlmRouter.build_chain(expert_id)` ; si lien
courant lève `ProviderError(retryable=True)` après retry policy
épuisé OU `CircuitOpenError`, passe au lien suivant. Chaîne complète
échouée → SSE `event: error LLM_UNAVAILABLE` puis `event: done`.

---

## Économie & sécurité

### BudgetTracker (Redis)

4 méthodes atomiques INCR + DECR rollback :
- `check_and_consume_chat` (50/jour Free, 1000/jour Pro)
- `check_and_consume_image` (3/jour Free, 30/jour Pro)
- `check_and_consume_ip_burst` (20/min/IP)
- `check_and_consume_model` (cap modèle 100k/jour)
+ `check_and_consume_embeddings`, `_voice_minutes`, `_tts_chars`,
`_vision_images` (tier user-based).

Clés UTC : `budget:user:{uid}:chat:{YYYY-MM-DD}`. Reset à minuit UTC.
Fail-open si Redis down (anti-cascade panne).

### PromptCache (Redis)

Clé canonique SHA-256 sur `(model, messages, system_prompt,
temperature, max_tokens, expert_id)` JSON déterministe. TTL 24h. **Skip
safety-critical** (medicine/legal — réponses sensibles, jamais
rejouées) + skip multi-turn user (contexte conversationnel).
Header HTTP `X-Cache: HIT|MISS|BYPASS`.

### TokenEstimator

`tiktoken` réel pour OpenAI (`o200k_base`) + Qwen (`cl100k_base`),
heuristique `chars/3.0 × 1.15 + overhead` Gemini/Anthropic (tokenizers
non publics). Cap `chat_prompt_tokens_per_request_max=30_000` →
402 `LLM_QUOTA_EXCEEDED` **avant** appel provider (anti-abus).

### ModerationService

OpenAI `omni-moderation-latest`. Fail-open 3s sur erreurs transport.
Désactivé si `openai_api_key` vide (warning unique boot).

### moderation_rules

Regex métier FR :
- 4 patterns prescription nominative (« prescris-moi 40 mg X »,
  « combien de mg de Y prendre »...)
- 3 patterns rédaction acte juridique nominatif (« rédige bail entre
  Jean et SARL »)

Whitelist par expert vide V1 (même medicine/legal refusent prescription
nominative). Kill-switch `moderation_rules_enabled`.

---

## SSE streaming

### `StreamHandler.stream(request, ctx)`

Générateur SSE production-grade :
- Heartbeat `: keepalive` toutes les 15s (anti-coupure proxy 2G/3G)
- Annulation duale :
  - `Request.is_disconnected()` toutes les 2s
  - Clé Redis `chat:cancel:{session_id}` toutes les 1s
- Traversée fallback chain
- Premier chunk préfixé du disclaimer si l'expert en a un

### Format événements SSE

```
event: chunk
data: {"delta": "Bonjour"}

: keepalive

event: tool_call
data: {"id": "call_abc", "name": "create_task", "arguments_json_partial": "{...}"}

event: done
data: {"reason": "stop"}
```

### Cost tracking fire-and-forget

Fin de stream :
```python
cost_tracker.record_ai_call_background(
    user_id=..., session_id=..., trace_id=...,
    provider="gemini", model="gemini-2.5-flash",
    prompt_tokens=..., completion_tokens=...,
    cost_usd=Decimal("0.000123"),
    outcome="completed",  # | "cancelled" | "failed"
)
```

`asyncio.create_task` — le SSE ne bloque JAMAIS sur l'écriture DB.
INSERT `ai_calls` + UPSERT `usage_daily` (UPSERT uniquement si
`outcome ∈ {completed, cancelled}`).

---

## SessionStore (filet sécurité)

Au cas où le fast path `cost_tracker.record_ai_call_background` crash
(worker OOM entre fin SSE et INSERT DB) :

```
Stream end → SET ai:session:{session_id} TTL 24h (Redis)
               → CostTracker async task → INSERT ai_calls
                  → DELETE Redis key

Cron flush_ai_sessions toutes les 10min :
  SCAN ai:session:* → INSERT ON CONFLICT (session_id) DO NOTHING
                    → DELETE Redis key
```

`ai_calls.session_id UNIQUE` garantit zéro double-facturation même si
le filet re-tente plusieurs fois.

---

## Function calling (F2 + F2.5)

### Architecture

```python
ToolDefinition(name, description, parameters_schema, handler)
ToolRegistry.register(tool_def)  # singleton process-wide
```

4 tools Planner enregistrés au lifespan :
- `create_task` / `list_tasks` / `update_task` / `pause_task`

### Orchestrateur `run_with_tool_rounds`

Boucle multi-rounds (cap `chat_max_tool_rounds=5` anti-boucle) :
1. Stream LLM → collect `tool_call` deltas par index
2. Si finish=`tool_calls` → `execute_tool_call(tc, registry, user, db)`
3. Inject `[TOOL RESULT id=... name=...] {json}` en role=user
4. Re-stream avec contexte enrichi
5. Stop quand finish=`stop` ou cap atteint

### Wiring providers

Chaque provider mappe `request.tools` au format SDK natif :
- OpenAI : `tools=[{type:function, function:{...}}]` (passage direct)
- Anthropic : `tools=[{name, description, input_schema}]` (helper `_to_anthropic_tools`)
- Gemini : `tools=[{function_declarations: [...]}]` (helper `_to_gemini_tools`)
- Qwen : OpenAI compat (passage direct)

### Safety

`tools_allowed=False` sur `medicine`/`legal` (cf. ExpertConfig). Le
router `/chat/stream` injecte `tools` dans `StreamContext` UNIQUEMENT
si `settings.tools_enabled_in_chat AND config.tools_allowed`.

---

## Mock-first SaaS pattern

8 intégrations suivent le pattern signature NEXYA :

| Service | Real client | Mock client | Trigger mock |
|---|---|---|---|
| Brevo (A1) | `BrevoEmailService` httpx | `MockEmailService` accumule | `BREVO_API_KEY=""` |
| hCaptcha (A3) | `HCaptchaVerifier` httpx | `MockCaptchaVerifier` | `HCAPTCHA_SECRET_KEY=""` ou `HCAPTCHA_ENABLED=False` |
| FCM (F2) | `FirebaseFCMProvider` `google-auth` | `MockFCMProvider` | `FCM_SERVICE_ACCOUNT_*=""` |
| Vision (E2) | Gemini/OpenAI vision SDK | `MockVisionProvider` | `OPENAI_API_KEY=""` ou `VISION_MOCK_ENABLED=true` |
| Voice (E1) | Whisper STT + OpenAI TTS | `MockVoiceProvider` | `OPENAI_API_KEY=""` ou `VOICE_MOCK_ENABLED=true` |
| Embeddings (D1/G1) | OpenAI/Gemini embeddings | `MockEmbeddingsProvider` SHA L2-norm | `EMBEDDINGS_MOCK_ENABLED=true` |
| Crisp (N4 Phase 18) | `RealCrispClient` httpx | `MockCrispClient` accumule | `CRISP_API_KEY=""` |
| MinIO/S3 (C3 + E3) | `S3ObjectStore` aioboto3 | `MockObjectStore` dict in-memory | `STORAGE_MOCK_ENABLED=true` |

**Bénéfices** :
- Dev local sans aucune clé API
- CI sans secret (`pytest tests/` tourne 100%)
- Tests déterministes (mocks stateless ou SHA-based)
- Switch instantané réel ↔ mock par variable d'env (pas de code change)

---

## Observabilité IA

### `StreamMetrics` (accumulateur per-stream)

Champs collectés en bout-en-bout :
`user_id, trace_id, expert_id, session_id, provider, model,
first_chunk_ms, total_duration_ms, prompt_tokens, completion_tokens,
total_tokens, cost_usd, outcome, failure_code, attempts, fallback_used`

### Log unique `ai.chat.completed`

Émis en fin de stream avec tous les champs ci-dessus → indexable par
trace_id côté Sentry/Loki.

### Prometheus (K1)

14 métriques `nexya_ai_*` :
- `nexya_ai_chat_calls_total{provider, model, outcome}` Counter
- `nexya_ai_chat_first_chunk_seconds` Histogram (TTFT)
- `nexya_ai_chat_total_duration_seconds` Histogram
- `nexya_ai_tokens_consumed_total{kind}` Counter
- `nexya_ai_cost_usd_total{provider, model}` Counter
- `nexya_ai_provider_failures_total{provider, model, error_type}` Counter
- `nexya_ai_circuit_breaker_state{provider, model}` Gauge (0/1/2)

Voir [`observability.md`](observability.md) pour le détail K1+K2.

---

## Évals IA reproductibles (N3)

`tests/evals/` harness Python pur (~700 lignes) qui détecte les
régressions de qualité IA introduites par un PR (changement prompt,
modèle, fallback, SDK) avant prod.

5 catégories × ~130 prompts versionnés YAML :
- `routing` — pure introspection EXPERT_REGISTRY
- `safety` — refus prescriptions/actes nominatifs + jailbreaks doux
- `format` — code blocks, LaTeX, listes numérotées
- `accuracy` — faits vérifiables par expert
- `identity` — marque NEXYA jamais cassée

Juge `MockJudge` SHA déterministe (CI gratuit) ou `GeminiJudge`
structured output (nightly real cost ~$30/mois). Baseline gelée
`tests/evals/baselines/baseline.json` committée. Workflow
`.github/workflows/evals.yml` 2 jobs (PR mock + nightly real, fail
+open issue auto si régression > seuil pp).

Voir [`tests/evals/README.md`](../../tests/evals/README.md).
