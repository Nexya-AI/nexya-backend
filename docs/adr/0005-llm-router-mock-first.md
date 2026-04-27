# ADR 0005 — LlmRouter mock-first SaaS pattern

## Status

Accepted (2026-04-22)

## Context

NEXYA dépend de **8 services SaaS externes** (au 2026-04-27) :
- Google Gemini (LLM principal)
- OpenAI (LLM fallback + modération + embeddings + Whisper STT + TTS)
- Anthropic (LLM fallback)
- Qwen DashScope (LLM fallback)
- Brevo (emails transactionnels)
- hCaptcha (captcha auth)
- Firebase FCM (push notifications)
- Crisp (support chat — Phase 18)

+ MinIO/S3 (object storage).

Problèmes :
1. **Dev sans clé** : nouveau dev arrive lundi, doit cloner et
   `docker compose up`. Si chaque clé API est requise pour démarrer,
   il passe sa journée à demander des accès.
2. **CI sans secret** : un PR qui casse pas backend ne devrait pas
   nécessiter `OPENAI_API_KEY` GitHub secret pour passer les tests.
3. **Tests déterministes** : un test qui appelle un vrai LLM est
   flaky (variance, rate limits, coûts) et non-reproductible.
4. **Coûts dev/test** : facturer Gemini pour chaque pytest run = $$$
   inutile.

Choix entre :
1. Pattern **mock-first** : si clé absente, instancier un Mock qui
   usurpe l'identité du vrai service.
2. **Feature flag binaire** `USE_MOCK_AI=true` qui force tous les mocks.
3. **Decorator de test** `@pytest.mark.uses_real_api` skipif.
4. **Fixtures pytest** dédiées qui mock chaque test.

## Decision

**Mock-first auto par clé API**.

## Consequences

### Positives

- **Dev sans clé** marche : `cp .env.example .env` puis `docker
  compose up` → toute l'app fonctionne avec mocks. UX onboarding
  parfaite.
- **CI gratuite** : 4500+ tests pytest tournent sans aucun secret
  GitHub. Coût Gemini/OpenAI = $0 sur les PRs.
- **Tests déterministes** : MockChatProvider yield des chunks
  scriptés ; MockJudge SHA-256 ; MockEmbeddingsProvider L2-normalisé
  déterministe.
- **Switch instantané réel ↔ mock** : Ivan ajoute une clé dans `.env`
  → restart uvicorn → real provider auto-instancié, fallback chain
  fonctionne sans toucher au code.
- **Identité usurpée** : `MockChatProvider(name="openai",
  default_model="gpt-4o", supported_models=...)` — la fallback chain
  `experts.py` continue de résoudre vers les bons modèles, pas de
  warning `model_not_in_supported_set`.
- **Pattern signature NEXYA** appliqué à 8 services + MinIO = 9
  intégrations cohérentes.

### Négatives

- **Faux sentiment de sécurité** : un test mock-only peut passer
  alors qu'un vrai bug d'intégration provider existe
- **Couverture qualité réelle** = nightly évals N3 + load tests N4
  sont indispensables (cf. mitigations)
- **Mocks à maintenir** : si OpenAI change son API, le Mock peut
  devenir inconsistant avec le réel

### Mitigations

- **Évals IA reproductibles N3** : `tests/evals/` nightly cron 3h UTC
  avec **vrai juge Gemini** sur ~130 prompts. Détecte régressions
  qualité réelle.
- **Live smoke tests** dans `tests/test_providers_b1.py` taggés
  `@pytest.mark.skipif(not API_KEY)` — Ivan les lance manuellement
  avant chaque release.
- **Load tests N4** : k6 contre stack docker-compose mock-first
  (HTTP/DB/Redis perf), pas la latence Gemini (qui dépend de Google).
- **Warning unique au boot** : log explicite « ⚠️ XXX en mode Mock »
  pour ne pas oublier qu'on est en mock.

## Implémentation

### Pattern factory

```python
# app/ai/router.py
def build_default_router() -> LlmRouter:
    real = _build_real_chat_providers()       # selon clés présentes
    mocks = _build_mock_chat_providers()      # tous, identité usurpée

    chat_providers = {}
    for name in ("gemini", "openai", "anthropic", "qwen", "openrouter"):
        chat_providers[name] = real.get(name) or mocks[name]
    ...
```

### Liste des mocks NEXYA

| Service | Mock class | Trigger |
|---|---|---|
| ChatProvider | `MockChatProvider(name="...", default_model="...")` | `<PROVIDER>_API_KEY=""` |
| Email | `MockEmailService` | `BREVO_API_KEY=""` |
| Captcha | `MockCaptchaVerifier` | `HCAPTCHA_SECRET_KEY=""` ou `HCAPTCHA_ENABLED=False` |
| FCM | `MockFCMProvider` | `FCM_SERVICE_ACCOUNT_*=""` |
| Vision | `MockVisionProvider` | `OPENAI_API_KEY=""` ou `VISION_MOCK_ENABLED=true` |
| Voice | `MockVoiceProvider` | `OPENAI_API_KEY=""` ou `VOICE_MOCK_ENABLED=true` |
| Embeddings | `MockEmbeddingsProvider` | `EMBEDDINGS_MOCK_ENABLED=true` |
| Crisp | `MockCrispClient` | `CRISP_API_KEY=""` |
| Object Store | `MockObjectStore` (dict in-memory) | `STORAGE_MOCK_ENABLED=true` |
| Judge IA | `MockJudge` (SHA-256 déterministe) | `--judge=mock` CLI |

### Test du pattern

`tests/test_providers_b1.py` valide :
- `MockChatProvider` usurpe correctement (`name` + `default_model` +
  `supported_models` du vrai provider)
- Factory `build_default_router()` retourne Mock si clé vide,
  Real si clé présente
- Live smoke tests gated par `@pytest.mark.skipif(not OPENAI_API_KEY)`

## Alternatives considérées

### Feature flag binaire `USE_MOCK_AI=true`

**Pour** : 1 flag global pour tout activer/désactiver.

**Contre** :
- Tout-ou-rien : si on veut Gemini réel mais OpenAI mock (cas
  courant : Ivan a Gemini key mais pas OpenAI), pas possible
- Coût migration env var pour chaque switch
- Granularité absente

### Decorator pytest `@uses_real_api`

**Pour** : explicit dans les tests.

**Contre** :
- Force chaque dev à savoir QUEL test nécessite QUELLE clé
- Pas applicable hors tests (dev local sans clé doit pouvoir runner
  l'app)

### Fixtures pytest dédiées

**Pour** : isolation tests parfaite.

**Contre** :
- Tonnes de boilerplate (chaque test mock chaque service)
- Ne résout pas le cas dev local

## Notes

V2 — envisager :
- **Mock fidelity testing** : un test périodique qui vérifie que
  `MockChatProvider.stream_chat` produit le même format SSE que le
  vrai (anti-drift)
- **Recording mode** : capturer les réponses Gemini réelles + replay
  via Mock (similaire à VCR.py) — V3 si besoin de tests intégration
  reproductibles avec vrai LLM

V2 — recommandation Ivan post-launch : **garder le mock-first** comme
pattern de base, et ajouter des tests intégration `tests/integration/`
gated qui tournent contre les vrais services (en CI nightly seulement).
