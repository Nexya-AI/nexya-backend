# PROMPT DE RÉMÉDIATION — NEXYA BACKEND (Session du 2026-05-01)

> **Suite directe de l'audit `AUDIT_BACKEND_2026-05-01.md`**.
> 5 tâches priorisées : 3 fixes code + 2 préparations infra.
> Durée estimée : 7h. Lecture-écriture autorisée. **Aucune modification d'infrastructure prod** (pas de déploiement, pas d'exécution sur Hetzner — uniquement code, config, tests, docs).

---

## 0. Identité & Posture de l'exécutant

Tu es un **Staff Software Engineer** chargé d'exécuter les 5 corrections priorisées par l'audit du 2026-05-01. Tu opères dans `nexya_backend/` sur Windows. Discipline obligatoire :

1. **Tu lis les fichiers AVANT de les modifier.** Aucun edit aveugle. Pour chaque tâche, tu commences par un `Read` complet du fichier cible et des fichiers connexes (test, schémas, exceptions).
2. **Tu n'introduis JAMAIS de régression.** À la fin de CHAQUE tâche, tu lances `pytest` sur les tests touchés + une suite ciblée. Si une régression apparaît, tu fixes AVANT de continuer.
3. **Tu suis la règle `feedback_french_quality.md`** : français impeccable dans les docstrings, commentaires, messages d'erreur, runbooks.
4. **Tu suis la règle `feedback_git_commits.md`** : pas de `Co-Authored-By: Claude`, pas de préfixes Conventional Commits (`feat:`, `chore:`, etc.).
5. **Tu suis la règle `feedback_docs_update.md`** : MAJ obligatoire des fichiers docs avant annonce de fin (CLAUDE.md §15 entrée du jour, ROADMAP, COURS).
6. **Tu ne crées AUCUN fichier non explicitement listé** dans ce prompt. Pas de prolifération.
7. **Tu écris le code que tu aurais écrit toi-même.** Pas de placeholders `# TODO: implémenter`. Pas de `pass` cosmétique. Tout doit être complet.
8. **Tu ne déploies RIEN.** Les tâches 4 et 5 sont des **préparations** : tu écris du code/config qui sera utilisé au moment du déploiement L2 staging futur. Tu peux tester localement Docker mais tu ne touches à aucune ressource externe (Hetzner, S3, Cloudflare, etc.).

---

## 1. Contexte projet (rappel court)

NEXYA backend est une API FastAPI Python 3.12+ qui cible 950k → 9M utilisateurs.

**Stack pertinente pour cette session** :
- FastAPI + uvicorn + SQLAlchemy 2.0 async + psycopg 3.2 + Redis 5.2 + arq
- Pydantic v2 + structlog + JWT RS256 (PyJWT)
- Tests pytest + AsyncMock + dependency_overrides
- Postgres pgvector pg16 (Docker dev port 5433)
- 200 settings dans `app/config.py`, 1583 fonctions de tests

**Discipline existante à respecter** :
- Routers délèguent au service, jamais de logique métier dans `router.py` (CLAUDE.md §8).
- Toutes les réponses sont `NexyaResponse[T]` ou `Response(204)` (DELETE) ou `StreamingResponse` (SSE).
- 404 IDOR-safe partout (jamais 403 sur owner check — anti-énumération).
- Fail-safe absolu sur observabilité, notifications, library autosave, cost tracking : exception swallow + log warning, jamais raise.
- Mock-first sur 10 SaaS — pattern signature.
- Production safety guard `_enforce_production_safety` dans `app/config.py:840-929` — fail-fast au boot prod si config laxiste.

---

## 2. Vue d'ensemble des 5 tâches

| # | Titre | Type | Effort | Fichiers principaux | Sévérité audit |
|---|---|---|---|---|---|
| 1 | `/auth/refresh` rate limit IP | **Fix code** | 1h | `auth/router.py`, `rate_limiter.py`, `tests/` | **S0** |
| 2 | Cap `max_tokens` par défaut sur experts | **Fix code** | 1h | `ai/experts.py`, `tests/` | **S1** |
| 3 | Blacklist JWT alert si Redis down | **Fix code** | 1h | `auth/jwt.py`, `prometheus.py`, `tests/` | **S1** |
| 4 | Préparer `scripts/backup_db.sh` + runbook | **Préparation** | 2h | `scripts/`, `docs/runbooks/db-restore.md` | S1 |
| 5 | Préparer `docker/pgbouncer/` config + runbook | **Préparation** | 2h | `docker/pgbouncer/`, `docs/runbooks/deployment-l2.md` | S0 |

**Total : 7h.** Ordre d'exécution conseillé : 1 → 2 → 3 → 4 → 5 (du plus simple/critique au plus structurel).

**À la fin de chaque tâche** :
- Tests passent localement (`pytest tests/test_<X>.py -v`)
- 0 régression sur le périmètre existant
- Commit propre **sans** `Co-Authored-By` ni Conventional Commits

---

## 3. TÂCHE 1 — `/auth/refresh` rate limit IP (S0, 1h)

### Objectif

Aujourd'hui, `POST /auth/refresh` n'a **aucun** rate limit IP. Un attaquant qui obtient un refresh token leaké (XSS, vol device, MITM, log fuite) peut spammer la rotation JWT pour obtenir N access tokens **sans plafond**, en parallèle d'un brute-force sur d'autres comptes.

**Fix** : ajouter un rate limit IP `20 requêtes/minute/IP` sur `POST /auth/refresh`. Calibrage : 20/min couvre un user mobile qui fait plusieurs hot-reload Flutter + une rotation périodique sans gêner. Au-delà = abus évident.

### Fichiers à modifier

1. `app/core/security/rate_limiter.py` — ajouter `rate_limit_refresh(request)` helper
2. `app/features/auth/router.py` — appeler le helper au début de `refresh()`
3. `tests/test_auth_hardening.py` — ajouter 2 tests (happy path + 429)

### Méthode (étape par étape)

#### Étape 1.1 — Lire les fichiers

```
Read app/core/security/rate_limiter.py
Read app/features/auth/router.py
Read tests/test_auth_hardening.py (toute la structure pour le pattern)
```

Identifier :
- Le helper `rate_limit_login(request)` ligne 89 — pattern modèle à suivre
- Le helper `check_ip_rate_limit(request, action, max_requests, window_seconds)` ligne 49 — fonction sous-jacente
- Le pattern de test pour `RateLimitIPException` (regarder comment `test_login_rate_limited` est écrit s'il existe, sinon adapter le pattern de `test_password_reset.py`)

#### Étape 1.2 — Ajouter `rate_limit_refresh`

Dans `app/core/security/rate_limiter.py`, **APRÈS** `rate_limit_login`, **AVANT** la section `# RATE LIMIT — user-scoped`, ajouter :

```python
async def rate_limit_refresh(request: Request) -> None:
    """Rate limit pour POST /auth/refresh — 20 requêtes/minute par IP.

    Protège contre un attaquant qui obtient un refresh token leaké et
    spamme la rotation JWT pour obtenir N access tokens. 20/min couvre
    largement un usage légitime (un user mobile peut rotater plusieurs
    fois si l'app fait du hot-reload, mais jamais 20× en 60s).

    Calibration plus haute que `rate_limit_login` (10/min) parce qu'un
    refresh est moins coûteux qu'un login (pas de bcrypt, pas de SELECT
    user complet) et que les apps mobiles peuvent légitimement faire
    plusieurs refresh rapprochés sur un changement de réseau.
    """
    await check_ip_rate_limit(request, action="refresh", max_requests=20)
```

#### Étape 1.3 — Câbler dans le router

Dans `app/features/auth/router.py` :

(a) Ajouter `rate_limit_refresh` à l'import existant ligne 28-34 :
```python
from app.core.security.rate_limiter import (
    rate_limit_forgot_password_ip,
    rate_limit_login,
    rate_limit_refresh,  # ← AJOUTER
    rate_limit_register,
    rate_limit_register_daily_ip,
    rate_limit_reset_password_ip,
)
```

(b) Modifier `refresh()` ligne 193-200 — ajouter `request: Request` au signature ET appeler le rate limit AVANT le service :

```python
@router.post("/auth/refresh", response_model=NexyaResponse[TokenResponse])
async def refresh(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Renouvellement du couple access + refresh via rotation.

    Rate limit IP : 20/min. Un attaquant qui obtient un refresh token
    leaké ne peut pas l'exploiter pour spammer la rotation au-delà
    de 20× par minute par IP source.
    """
    await rate_limit_refresh(request)
    tokens = await auth_service.refresh(body.refresh_token, db)
    return NexyaResponse(success=True, data=tokens)
```

#### Étape 1.4 — Écrire les tests

Dans `tests/test_auth_hardening.py`, ajouter une nouvelle classe ou des fonctions de test à la fin du fichier (lire d'abord le pattern utilisé pour les autres rate limits — typiquement `monkeypatch` sur `redis_client.incr` pour simuler le compteur) :

**Test 1** — happy path : un appel à `/auth/refresh` passe à travers le rate limit et appelle bien le service :
```python
async def test_refresh_rate_limit_allows_under_threshold(...):
    """Sous le seuil 20/min, /auth/refresh appelle bien le service."""
    # Setup : monkeypatch auth_service.refresh pour retourner un TokenResponse fake
    # Mock le redis incr pour retourner 1, 2, 3 (sous le seuil)
    # Faire 3 POST /auth/refresh
    # Asserter : 3× 200, service appelé 3 fois
```

**Test 2** — 429 : le 21ᵉ appel dans la fenêtre lève `RATE_LIMIT_IP` :
```python
async def test_refresh_rate_limit_blocks_over_threshold(...):
    """Au-delà de 20/min, /auth/refresh retourne 429 RATE_LIMIT_IP."""
    # Mock redis incr pour retourner 21
    # Faire 1 POST /auth/refresh
    # Asserter : 429, code "RATE_LIMIT_IP", data.retry_after > 0
    # Asserter : auth_service.refresh JAMAIS appelé (rate limit AVANT service)
```

**Pattern existant à reproduire** : regarder `test_password_reset.py::test_router_reset_password_rate_limited` (s'il existe) ou `test_auth_hardening.py` pour le pattern monkeypatch redis. Si aucun pattern n'existe pour les routes auth, utiliser `app.dependency_overrides[get_db] = lambda: AsyncMock()` + `monkeypatch.setattr(auth_service, "refresh", AsyncMock(return_value=fake_tokens))` + `monkeypatch.setattr("app.core.security.rate_limiter.get_redis", lambda: fake_redis)`.

#### Étape 1.5 — Exécuter les tests

```bash
pytest tests/test_auth_hardening.py -v -k "refresh_rate_limit" --tb=short
```

Vérifier : 2 nouveaux tests verts. Lancer aussi la suite complète `tests/test_auth_hardening.py` pour s'assurer qu'aucun test existant ne casse.

### Critères d'acceptation tâche 1

- [ ] `rate_limit_refresh()` ajouté dans `rate_limiter.py` avec docstring complète
- [ ] Import + appel dans `auth/router.py:refresh()` avant `auth_service.refresh()`
- [ ] Signature `refresh()` étendue avec `request: Request`
- [ ] 2 nouveaux tests verts dans `test_auth_hardening.py`
- [ ] Suite complète `test_auth_hardening.py` 0 régression
- [ ] Suite complète `test_password_reset.py` + `test_auth_hardening_a3.py` 0 régression
- [ ] Le 21ᵉ appel sur la même IP dans 60s retourne 429 avec `code=RATE_LIMIT_IP` et `data.retry_after`

### Anti-patterns à éviter

- ❌ Ne PAS écrire un nouveau `check_ip_rate_limit` — réutiliser celui qui existe
- ❌ Ne PAS poser le rate limit AVANT le `Depends(get_db)` (FastAPI ordonne les dépendances)
- ❌ Ne PAS oublier le `request: Request` dans la signature — FastAPI ne peut pas l'injecter sans déclaration
- ❌ Ne PAS modifier `auth_service.refresh` — c'est le router qui rate-limit, pas le service

---

## 4. TÂCHE 2 — Cap `max_tokens` par défaut sur experts (S1, 1h)

### Objectif

Aujourd'hui, `ExpertConfig.max_tokens: int | None = None` (`app/ai/experts.py:48`). Sur les 11 experts (general + 10), aucun ne pose explicitement `max_tokens`. Conséquence : Gemini Pro peut générer 8000+ tokens en sortie sur un prompt mal formé. Calcul Rule G worst-case :

- Gemini 2.5 Pro output : **$5/1M tokens output**
- Output runaway 8000 tokens × $5/1M = **$0.04 par réponse runaway**
- 950k users × 50 chats/jour × 1% runaway = 475k réponses runaway/jour × $0.04 = **$19 000/jour worst-case**

**Fix** : poser un `max_tokens` raisonnable par défaut sur chaque expert. Le provider transmet `request.max_tokens` à l'API LLM (`OpenAIChatProvider:204`, `GeminiChatProvider:158`) — donc poser `max_tokens` sur l'`ExpertConfig` se propage automatiquement via `StreamHandler:558` qui fait `max_tokens=ctx.max_tokens or config.max_tokens`.

### Calibration recommandée

| Tier | Experts | `max_tokens` | Justification |
|---|---|---|---|
| `flash` | general, computer, finance, cooking, productivity | **2048** | Réponses concises typiques, cap équivalent à ~5 pages |
| `pro` | science, language, engineering | **4096** | Raisonnement multi-étapes (LaTeX, conjugaisons, calculs ingénierie) |
| `pro` safety-critical | medicine, legal | **3072** | Disclaimers + info structurée mais pas de génération créative |
| `image` | studio | **2048** | Non utilisé (image-only) mais cohérence si jamais Studio sert du texte |

### Fichiers à modifier

1. `app/ai/experts.py` — poser `max_tokens=N` sur les 11 entrées du `EXPERT_REGISTRY`
2. `tests/test_experts_registry.py` — ajouter 2 tests (cap présent + valeurs cohérentes)

### Méthode

#### Étape 2.1 — Lire les fichiers

```
Read app/ai/experts.py
Read app/ai/streaming.py:550-565 (vérifier comment max_tokens descend dans la chaîne)
Read tests/test_experts_registry.py
```

#### Étape 2.2 — Modifier `EXPERT_REGISTRY`

Pour chaque entrée du dict (lignes 285-444), ajouter `max_tokens=<valeur>` selon la grille ci-dessus, **après** `temperature` et **avant** `tier`.

Exemple pour `general` :
```python
"general": ExpertConfig(
    expert_id="general",
    display_name="Général",
    is_coming_soon=False,
    primary_provider="gemini",
    primary_model="gemini-2.5-flash",
    fallback_chain=(_GEMINI_PRO, _OPENROUTER_SONNET),
    system_prompt=_GENERAL_PROMPT,
    temperature=0.7,
    max_tokens=2048,  # ← AJOUTER
    tier="flash",
    tags=("general", "conversation"),
),
```

Appliquer aux 11 entrées avec les valeurs de la grille.

#### Étape 2.3 — Tests

Dans `tests/test_experts_registry.py`, ajouter :

```python
def test_all_experts_have_max_tokens_cap():
    """Chaque ExpertConfig doit poser max_tokens explicitement (anti-runaway facture)."""
    from app.ai.experts import EXPERT_REGISTRY

    for expert_id, config in EXPERT_REGISTRY.items():
        assert config.max_tokens is not None, (
            f"Expert '{expert_id}' n'a pas de max_tokens — risque output runaway "
            f"(Gemini Pro peut générer 8000+ tokens × $5/1M = facture explosée)"
        )
        assert config.max_tokens > 0
        assert config.max_tokens <= 8192, (
            f"Expert '{expert_id}' max_tokens={config.max_tokens} suspect "
            f"(au-delà de 8192 = créativité débridée non justifiée)"
        )


def test_max_tokens_aligned_with_tier():
    """Le cap max_tokens doit être cohérent avec le tier (flash plus serré que pro)."""
    from app.ai.experts import EXPERT_REGISTRY

    flash_caps = [c.max_tokens for c in EXPERT_REGISTRY.values() if c.tier == "flash"]
    pro_caps = [c.max_tokens for c in EXPERT_REGISTRY.values() if c.tier == "pro"]

    if flash_caps and pro_caps:
        assert max(flash_caps) <= max(pro_caps), (
            "Un expert tier=flash a max_tokens > tier=pro — incohérent"
        )
```

#### Étape 2.4 — Exécuter

```bash
pytest tests/test_experts_registry.py -v
```

Vérifier : suite complète passe + 2 nouveaux tests verts. Lancer aussi `tests/test_llm_router.py` (qui consomme `EXPERT_REGISTRY` indirectement) pour 0 régression.

### Critères d'acceptation tâche 2

- [ ] 11 entrées de `EXPERT_REGISTRY` ont `max_tokens=N` explicite avec valeur de la grille
- [ ] Aucun expert n'a `max_tokens=None` (à vérifier par grep `max_tokens` dans `experts.py`)
- [ ] 2 nouveaux tests verts dans `test_experts_registry.py`
- [ ] Suite complète `test_experts_registry.py` + `test_llm_router.py` 0 régression
- [ ] Provider OpenAI/Gemini transmet correctement le cap (vérifié par lecture `app/ai/providers/`)

### Anti-patterns à éviter

- ❌ Ne PAS poser un cap arbitrairement élevé (ex: 16384) — défait l'objectif de la tâche
- ❌ Ne PAS toucher la classe `ExpertConfig` (le défaut `None` reste — un override explicite nul reste possible pour cas exotiques)
- ❌ Ne PAS modifier `streaming.py` ou les providers — la chaîne `ctx.max_tokens or config.max_tokens` fonctionne déjà
- ❌ Ne PAS oublier `studio` (image-only) — poser quand même 2048 pour cohérence

---

## 5. TÂCHE 3 — Blacklist JWT alert si Redis down (S1, 1h)

### Objectif

Aujourd'hui (`app/core/auth/jwt.py:102-106`), si Redis est down quand `is_token_blacklisted(jti)` est appelé, **l'exception remonte** côté `get_current_user` qui la transforme en 500. Ou pire — selon les versions du client `redis-py`, certains timeouts retournent `False` silencieusement → tokens blacklistés acceptés.

**Le risque réel** est :
1. Comportement non déterministe (raise vs False silencieux selon le mode d'erreur Redis)
2. Aucun signal côté observabilité quand ça arrive
3. Pas de métrique Prometheus pour alerter

**Fix** : envelopper `is_token_blacklisted` dans un try/except explicite, retourner `False` (fail-open documenté), logger un warning structuré + incrémenter une métrique Prometheus `nexya_auth_blacklist_check_failed_total`.

**Justification fail-open** : si Redis tombe pendant 30s pendant un incident, on préfère accepter quelques tokens potentiellement blacklistés (les attaquants visent rarement ce timing exact) plutôt que rejeter TOUS les utilisateurs légitimes (fail-closed = downtime massif). L'alerte Prometheus permettra à l'ops de réagir vite.

### Fichiers à modifier

1. `app/core/observability/prometheus.py` — ajouter la nouvelle métrique
2. `app/core/auth/jwt.py` — envelopper `is_token_blacklisted` avec try/except + log + métrique
3. `tests/test_auth_hardening.py` — ajouter 2 tests (Redis down → False + métrique inc, Redis OK → comportement inchangé)

### Méthode

#### Étape 3.1 — Lire les fichiers

```
Read app/core/observability/prometheus.py (lignes 1-280 pour comprendre le pattern)
Read app/core/auth/jwt.py
Read tests/test_auth_hardening.py
Read tests/test_prometheus_metrics.py (pour le pattern test métrique)
```

#### Étape 3.2 — Ajouter la métrique Prometheus

Dans `app/core/observability/prometheus.py` :

(a) Ligne 67, ajouter à la liste des module-level globals :
```python
auth_blacklist_check_failed_total: Any = None
```

(b) Ligne 79-85, ajouter à la liste `global` du `setup_prometheus()` :
```python
global auth_blacklist_check_failed_total
```

(c) Après `cache_operations_total` (ligne ~206), ajouter :
```python
auth_blacklist_check_failed_total = Counter(
    "nexya_auth_blacklist_check_failed_total",
    "Échecs de vérification JWT blacklist (Redis down/timeout/error). "
    "Fail-open : un échec ne refuse pas le token, mais loggue + alerte. "
    "Une valeur > 0 sur 5 min = Redis instable, intervention ops requise.",
    labelnames=("error_type",),
    registry=_REGISTRY,
)
```

(d) Mettre à jour le compteur de métriques ligne 211 : `metrics_count=14` (au lieu de 13).

(e) Ligne 235-262 (`_reset_for_tests`), ajouter à la liste globals + au reset :
```python
global auth_blacklist_check_failed_total
...
auth_blacklist_check_failed_total = None
```

(f) Ajouter le helper `record_auth_blacklist_check_failed` après `set_circuit_breaker_state` :
```python
def record_auth_blacklist_check_failed(error_type: str) -> None:
    """Hook depuis `is_token_blacklisted` quand Redis est inaccessible.

    `error_type` ∈ {redis_timeout, redis_connection, redis_unknown}.
    Fail-open : on continue à accepter le token, mais on alerte via
    cette métrique (un seuil > 0 sur 5 min déclenche l'alerte
    `NexyaAuthBlacklistDegraded` configurée dans Grafana).
    """
    if not _INIT_OK:
        return
    _safe_call(auth_blacklist_check_failed_total.labels(error_type=error_type).inc)
```

#### Étape 3.3 — Modifier `is_token_blacklisted`

Dans `app/core/auth/jwt.py`, remplacer la fonction `is_token_blacklisted` (lignes 102-106) par :

```python
async def is_token_blacklisted(jti: str) -> bool:
    """Vérifie si un access token est dans la blacklist Redis.

    **Fail-open documenté** : si Redis est inaccessible (timeout, connection
    refused, autre), on retourne `False` plutôt que de raise. Justification :

    - Fail-closed (= refuser tous les tokens si Redis down) provoquerait un
      downtime total pour TOUS les utilisateurs légitimes pendant un
      incident Redis transitoire (30s de blip).
    - Fail-open (ici) accepte transitoirement quelques tokens potentiellement
      blacklistés. Le risque réel est très faible : les blacklists
      contiennent les access tokens des users qui se sont déconnectés
      explicitement, pas des comptes compromis (ceux-là sont gérés via
      la rotation refresh + revoke RGPD).

    Une métrique Prometheus `nexya_auth_blacklist_check_failed_total` est
    incrémentée à chaque échec — l'alerte `NexyaAuthBlacklistDegraded`
    déclenche dès qu'on en voit > 5 sur 5 min, signal opérationnel pour
    investiguer Redis.
    """
    # Import paresseux pour éviter le cycle prometheus → jwt → prometheus
    from app.core.observability.prometheus import (  # noqa: PLC0415
        record_auth_blacklist_check_failed,
    )

    try:
        redis = get_redis()
        key = f"{BLACKLIST_PREFIX}{jti}"
        return await redis.exists(key) > 0
    except (TimeoutError, ConnectionError) as exc:
        # redis-py lève `redis.exceptions.TimeoutError` (subclass de TimeoutError
        # builtin) ou `redis.exceptions.ConnectionError` (subclass de OSError /
        # ConnectionError builtin). Les deux sont catchés ici.
        error_type = "redis_timeout" if isinstance(exc, TimeoutError) else "redis_connection"
        log.warning(
            "auth.blacklist.check_failed",
            jti=jti,
            error=str(exc),
            error_type=error_type,
            fallback="fail_open",
        )
        record_auth_blacklist_check_failed(error_type)
        return False
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "auth.blacklist.check_failed",
            jti=jti,
            error=str(exc),
            error_type="redis_unknown",
            exc_type=type(exc).__name__,
            fallback="fail_open",
        )
        record_auth_blacklist_check_failed("redis_unknown")
        return False
```

#### Étape 3.4 — Tests

Dans `tests/test_auth_hardening.py`, ajouter :

```python
async def test_blacklist_check_redis_timeout_returns_false_fail_open(monkeypatch):
    """Si Redis timeout, is_token_blacklisted retourne False (fail-open)."""
    from app.core.auth import jwt as jwt_module
    from unittest.mock import AsyncMock, MagicMock

    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(side_effect=TimeoutError("redis timeout"))
    monkeypatch.setattr("app.core.auth.jwt.get_redis", lambda: fake_redis)

    result = await jwt_module.is_token_blacklisted("some-jti")

    assert result is False, "Fail-open obligatoire — un timeout Redis ne doit pas casser l'auth"


async def test_blacklist_check_redis_down_increments_metric(monkeypatch):
    """Un échec Redis incrémente la métrique Prometheus."""
    from app.core.auth import jwt as jwt_module
    from app.core.observability import prometheus as prom_module
    from unittest.mock import AsyncMock, MagicMock

    # Reset + setup propre
    prom_module._reset_for_tests()
    from app.config import settings
    prom_module.setup_prometheus(settings)

    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(side_effect=ConnectionError("connection refused"))
    monkeypatch.setattr("app.core.auth.jwt.get_redis", lambda: fake_redis)

    await jwt_module.is_token_blacklisted("some-jti")

    # Vérifier que la métrique a été incrémentée pour error_type=redis_connection
    metric = prom_module.auth_blacklist_check_failed_total
    samples = list(metric.collect())[0].samples
    matching = [s for s in samples if s.labels.get("error_type") == "redis_connection" and s.value > 0]
    assert matching, "Métrique nexya_auth_blacklist_check_failed_total{error_type=redis_connection} doit être > 0"


async def test_blacklist_check_redis_ok_returns_correct_value(monkeypatch):
    """Si Redis OK, le comportement reste inchangé (vrai positif + vrai négatif)."""
    from app.core.auth import jwt as jwt_module
    from unittest.mock import AsyncMock, MagicMock

    fake_redis = MagicMock()
    fake_redis.exists = AsyncMock(return_value=1)  # token blacklisté
    monkeypatch.setattr("app.core.auth.jwt.get_redis", lambda: fake_redis)

    assert await jwt_module.is_token_blacklisted("blacklisted-jti") is True

    fake_redis.exists = AsyncMock(return_value=0)  # token non blacklisté
    assert await jwt_module.is_token_blacklisted("clean-jti") is False
```

#### Étape 3.5 — Exécuter

```bash
pytest tests/test_auth_hardening.py -v -k "blacklist_check"
pytest tests/test_prometheus_metrics.py -v  # 0 régression sur la métrique count
```

### Critères d'acceptation tâche 3

- [ ] Métrique `nexya_auth_blacklist_check_failed_total` ajoutée dans `prometheus.py` avec label `error_type`
- [ ] Compteur global `metrics_count=14` mis à jour ligne 211
- [ ] `_reset_for_tests()` reset la nouvelle métrique
- [ ] Helper `record_auth_blacklist_check_failed(error_type)` exposé
- [ ] `is_token_blacklisted` capture 3 cas (Timeout / Connection / Exception) avec `error_type` distinct
- [ ] Fail-open : retourne `False` à chaque cas + log warning + métrique inc
- [ ] 3 nouveaux tests verts (timeout, métrique inc, comportement nominal)
- [ ] Suite `test_auth_hardening.py` + `test_prometheus_metrics.py` 0 régression
- [ ] Si un test cherche `metrics_count == 13`, le mettre à jour vers 14

### Anti-patterns à éviter

- ❌ Ne PAS faire `except Exception` directement sans distinction — on perd la granularité error_type
- ❌ Ne PAS catch `BaseException` (laisse passer `KeyboardInterrupt`, `SystemExit`, `asyncio.CancelledError`)
- ❌ Ne PAS oublier l'import paresseux dans `is_token_blacklisted` (cycle potentiel jwt ↔ prometheus)
- ❌ Ne PAS faire fail-closed (= raise) — la justification fail-open est documentée et alignée avec d'autres composants NEXYA

---

## 6. TÂCHE 4 — Préparer `scripts/backup_db.sh` + runbook (S1, 2h)

### Objectif

Le runbook `docs/runbooks/db-restore.md` mentionne `scripts/backup_db.sh` mais le fichier n'existe pas. Cette tâche **crée le script** + **enrichit le runbook** + **teste localement contre Docker dev** (sans le déployer en prod).

**Important** : on ne déploie PAS le cron sur Hetzner. On prépare le code/runbook pour que le jour J du déploiement L2, c'est juste un `cp scripts/backup_db.sh /opt/nexya/ + crontab`.

### Fichiers à créer/modifier

1. **CRÉER** `scripts/backup_db.sh` — script bash strict
2. **CRÉER** `scripts/restore_db.sh` — script bash strict (procédure restore automatisée)
3. **MODIFIER** `docs/runbooks/db-restore.md` — référencer les scripts réels au lieu d'inline
4. **MODIFIER** `docs/runbooks/deployment-l2.md` — section "Backups DB" pointe vers script réel
5. **CRÉER** `tests/test_backup_scripts.py` — vérifications structurelles bash + dry-run

### Méthode

#### Étape 4.1 — Lire les fichiers existants

```
Read docs/runbooks/db-restore.md (intégralement)
Read docs/runbooks/deployment-l2.md (section backups)
Read scripts/release.sh (pattern bash strict NEXYA)
Read scripts/rollback.sh (idem)
Read scripts/smoke_test.sh (idem)
```

Identifier le pattern bash strict NEXYA :
- `set -euo pipefail`
- `trap` EXIT pour cleanup
- Validation des args / env vars
- `--dry-run` mode
- `[DRY-RUN]` prefix sur les commandes en mode dry-run
- Logging structuré (lignes `echo` avec timestamps)

#### Étape 4.2 — Créer `scripts/backup_db.sh`

Spec :
- Args : `--dry-run` (optionnel), pas d'autres args (lit env vars)
- Env vars requises : `BACKUP_DIR` (défaut `/backups`), `S3_BUCKET` (défaut `nexya-backups`), `POSTGRES_CONTAINER` (défaut `nexya-postgres`), `POSTGRES_DB` (défaut `nexya`), `POSTGRES_USER` (défaut `nexya`)
- Env vars optionnelles : `BACKUP_RETENTION_DAYS` (défaut `7`), `S3_REGION` (défaut `eu-central-1`), `BACKUP_GPG_RECIPIENT` (si défini, chiffre via gpg avant upload)
- Output : timestamp + chemin du dump créé
- Exit codes : 0 OK, 1 backup échoué, 2 args invalides, 3 dependencies manquantes
- Idempotent : 2 runs simultanés ne corrompent pas le dump (lock via `flock` sur `/var/lock/nexya-backup.lock`)

Structure attendue (à écrire **complètement**, pas de placeholder) :

```bash
#!/usr/bin/env bash
# scripts/backup_db.sh — Backup quotidien Postgres NEXYA → S3.
# ...docstring exhaustive...

set -euo pipefail

# ── Constants & defaults ──
LOCK_FILE="/var/lock/nexya-backup.lock"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
S3_BUCKET="${S3_BUCKET:-nexya-backups}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexya-postgres}"
POSTGRES_DB="${POSTGRES_DB:-nexya}"
POSTGRES_USER="${POSTGRES_USER:-nexya}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
S3_REGION="${S3_REGION:-eu-central-1}"
BACKUP_GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"

DRY_RUN="false"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
DUMP_FILENAME="nexya-${TIMESTAMP}.dump"
DUMP_PATH="${BACKUP_DIR}/${DUMP_FILENAME}"

# ── Logging ──
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
log_info() { log "INFO  $*"; }
log_error() { log "ERROR $*"; }

# ── Argparse ──
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    -h|--help) cat <<EOF
Usage: $0 [--dry-run]
... documentation complète ...
EOF
      exit 0 ;;
    *) log_error "Argument inconnu: $1"; exit 2 ;;
  esac
done

# ... reste du script complet ...
```

Sections obligatoires du script :
1. **Pré-checks** (deps : `docker`, `aws`, optionnel `gpg`, `flock`)
2. **Lock** (via `flock` non-bloquant — exit 1 si un autre backup tourne)
3. **Mkdir backup_dir** (idempotent)
4. **pg_dump** via `docker exec $POSTGRES_CONTAINER pg_dump --format=custom --compress=9 -U $POSTGRES_USER -d $POSTGRES_DB > $DUMP_PATH`
5. **SHA-256** : `sha256sum "$DUMP_PATH" > "${DUMP_PATH}.sha256"`
6. **GPG (optionnel)** : si `$BACKUP_GPG_RECIPIENT` défini, `gpg --encrypt --recipient $BACKUP_GPG_RECIPIENT --output ${DUMP_PATH}.gpg $DUMP_PATH` puis remplacer `$DUMP_PATH` par version chiffrée
7. **Upload S3** : `aws s3 cp $DUMP_PATH s3://$S3_BUCKET/$(date -u +%Y/%m)/ --sse AES256 --region $S3_REGION` (idem pour `.sha256`)
8. **Cleanup local > $BACKUP_RETENTION_DAYS jours** : `find $BACKUP_DIR -name "nexya-*.dump*" -mtime +$BACKUP_RETENTION_DAYS -delete`
9. **Rapport final** : taille du dump, durée, statut S3

En mode `--dry-run` :
- Préfixer chaque commande exécutable par `[DRY-RUN]` dans les logs
- Ne PAS exécuter `pg_dump`, `aws s3 cp`, `gpg`, `find -delete`
- Vérifier les pré-checks réels (docker daemon up, aws CLI installé, etc.)
- Exit 0 si tous les pré-checks passent

#### Étape 4.3 — Créer `scripts/restore_db.sh`

Spec :
- Args : `<dump_s3_path>` (obligatoire), `--target-db <name>` (optionnel, défaut `nexya_restore`), `--dry-run`, `--swap` (optionnel — si présent, swap nexya_restore → nexya après vérification)
- Env vars : mêmes que `backup_db.sh`
- Procédure : reproduire les étapes 1-12 du `db-restore.md` cas 1 dans un script automatisé
- Vérifications post-restore obligatoires : `count(users)`, `last_migration`, `count messages WHERE conversation_id NOT IN (SELECT id FROM conversations)` (doit être 0)
- Si `--swap`, attente confirmation user (sauf en dry-run) avant de renommer les DBs

Structure mirror `backup_db.sh` (logging, dry-run, exit codes).

#### Étape 4.4 — Modifier `docs/runbooks/db-restore.md`

Remplacer la section « Script `scripts/backup_db.sh` (à créer V2) » par :

```markdown
### Script `scripts/backup_db.sh` (livré 2026-05-01)

Script bash strict (`set -euo pipefail`) avec mode `--dry-run`,
chiffrement GPG optionnel (env `BACKUP_GPG_RECIPIENT`), lock via
`flock` (anti double-run), upload S3 SSE AES256.

Usage :
\`\`\`bash
# Cron quotidien (à installer en prod L2)
0 3 * * * deploy /opt/nexya/scripts/backup_db.sh

# Test manuel dry-run
bash scripts/backup_db.sh --dry-run

# Test manuel exécution réelle (en local Docker dev)
BACKUP_DIR=/tmp/backup-test \
S3_BUCKET=test-skip \  # skip upload si S3 absent
POSTGRES_CONTAINER=nexya-postgres \
bash scripts/backup_db.sh
\`\`\`

Voir [scripts/backup_db.sh](../../scripts/backup_db.sh) pour les détails.
```

Et remplacer la section restore inline par :

```markdown
### Restauration automatisée (livrée 2026-05-01)

Le script `scripts/restore_db.sh` automatise les étapes 1-12 :

\`\`\`bash
# Restauration ponctuelle vers DB temporaire (sans swap)
bash scripts/restore_db.sh s3://nexya-backups/2026/04/nexya-20260427_030001.dump

# Restauration + swap nexya_restore → nexya (avec confirmation)
bash scripts/restore_db.sh \
    s3://nexya-backups/2026/04/nexya-20260427_030001.dump \
    --swap

# Dry-run (validation chemin S3 + dépendances)
bash scripts/restore_db.sh \
    s3://nexya-backups/2026/04/nexya-20260427_030001.dump \
    --dry-run
\`\`\`

Voir [scripts/restore_db.sh](../../scripts/restore_db.sh) pour les détails.
```

Les sections "Cas 1" / "Cas 2" / "Drill quarterly" peuvent rester (procédures manuelles fallback documentées).

#### Étape 4.5 — Modifier `docs/runbooks/deployment-l2.md`

Section "Backups DB" : remplacer le `scripts/backup_db.sh (à créer V2)` par un lien vers le vrai script :

```markdown
### Backups DB (cron quotidien)

\`\`\`bash
# /etc/cron.d/nexya-backup
0 3 * * * deploy /opt/nexya/scripts/backup_db.sh
\`\`\`

Le script `scripts/backup_db.sh` est livré (2026-05-01) avec dry-run,
chiffrement GPG optionnel, lock anti-concurrent, upload S3 SSE AES256.
Configurer `BACKUP_GPG_RECIPIENT` dans `.env.production` pour activer
le chiffrement GPG (recommandé prod). Voir
[`db-restore.md`](db-restore.md) pour la procédure restore via
`scripts/restore_db.sh`.
```

#### Étape 4.6 — Tests structure

Créer `tests/test_backup_scripts.py` :

```python
"""Tests structurels des scripts backup_db.sh + restore_db.sh.

Ne lance PAS pg_dump réel — vérifie uniquement la structure du script
(strict bash, dry-run présent, args parsing, env vars utilisées).
Pattern aligné sur tests/test_rollback_script.py + test_smoke_test_script.py.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
BACKUP = ROOT / "scripts" / "backup_db.sh"
RESTORE = ROOT / "scripts" / "restore_db.sh"


@pytest.fixture
def bash_available():
    if shutil.which("bash") is None:
        pytest.skip("bash absent (Windows sans Git Bash)")


def test_backup_script_exists():
    assert BACKUP.exists(), "scripts/backup_db.sh doit exister"


def test_backup_script_strict_bash():
    content = BACKUP.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content


def test_backup_script_syntax_check(bash_available):
    """bash -n vérifie la syntaxe sans exécuter."""
    result = subprocess.run(
        ["bash", "-n", str(BACKUP)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_backup_script_has_dry_run_flag():
    content = BACKUP.read_text(encoding="utf-8")
    assert "--dry-run" in content
    assert "DRY_RUN" in content


def test_backup_script_dry_run_executes_without_side_effects(bash_available):
    """`--dry-run` doit exit 0 sans exécuter pg_dump ni aws s3 cp."""
    result = subprocess.run(
        ["bash", str(BACKUP), "--dry-run"],
        capture_output=True, text=True, timeout=10,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    # Exit 0 OU exit 3 (deps manquantes en CI/Windows) acceptés
    assert result.returncode in (0, 3), f"stderr: {result.stderr}"
    assert "[DRY-RUN]" in result.stderr or "[DRY-RUN]" in result.stdout


def test_backup_script_uses_env_vars():
    content = BACKUP.read_text(encoding="utf-8")
    for var in ("BACKUP_DIR", "S3_BUCKET", "POSTGRES_CONTAINER",
                "POSTGRES_DB", "POSTGRES_USER", "BACKUP_RETENTION_DAYS"):
        assert var in content, f"env var {var} doit être lue"


def test_backup_script_uses_pg_dump():
    content = BACKUP.read_text(encoding="utf-8")
    assert "pg_dump" in content
    assert "--format=custom" in content
    assert "--compress=9" in content


def test_backup_script_uses_sse_aes256():
    content = BACKUP.read_text(encoding="utf-8")
    assert "--sse AES256" in content


def test_backup_script_has_lock():
    content = BACKUP.read_text(encoding="utf-8")
    assert "flock" in content


def test_backup_script_supports_gpg():
    content = BACKUP.read_text(encoding="utf-8")
    assert "BACKUP_GPG_RECIPIENT" in content
    assert "gpg" in content


def test_restore_script_exists():
    assert RESTORE.exists()


def test_restore_script_strict_bash():
    content = RESTORE.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content


def test_restore_script_syntax_check(bash_available):
    result = subprocess.run(
        ["bash", "-n", str(RESTORE)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_restore_script_validates_post_restore():
    """Le script doit faire les vérifs post-restore (count users, last_migration, FK)."""
    content = RESTORE.read_text(encoding="utf-8")
    assert "count" in content.lower() and "users" in content
    assert "alembic_version" in content


def test_restore_script_supports_swap():
    content = RESTORE.read_text(encoding="utf-8")
    assert "--swap" in content
    assert "ALTER DATABASE" in content


def test_restore_script_supports_dry_run():
    content = RESTORE.read_text(encoding="utf-8")
    assert "--dry-run" in content
```

#### Étape 4.7 — Test manuel local (optionnel mais recommandé)

```bash
# Dry-run
bash scripts/backup_db.sh --dry-run

# Test réel sur Docker dev (S3 skip si pas configuré)
mkdir -p /tmp/backup-test
BACKUP_DIR=/tmp/backup-test \
S3_BUCKET=test-skip \
bash scripts/backup_db.sh
ls /tmp/backup-test/
# Doit voir : nexya-YYYYMMDD_HHMMSS.dump + .sha256
```

Si l'upload S3 échoue (pas de credentials), c'est OK — le script doit logger l'échec mais le dump local est créé.

### Critères d'acceptation tâche 4

- [ ] `scripts/backup_db.sh` créé, strict bash, dry-run, flock, gpg optionnel, sse aes256, retention configurable
- [ ] `scripts/restore_db.sh` créé, strict bash, dry-run, --swap, vérifs post-restore intégrité
- [ ] `tests/test_backup_scripts.py` créé avec ~14 tests structurels verts
- [ ] `docs/runbooks/db-restore.md` mis à jour pour pointer vers les scripts réels
- [ ] `docs/runbooks/deployment-l2.md` section backup mis à jour
- [ ] Test manuel local `bash scripts/backup_db.sh --dry-run` exit 0
- [ ] Suite `tests/test_backup_scripts.py` 100 % verte
- [ ] **AUCUN test backend cassé** (la suite globale reste à 1583 tests verts)

### Anti-patterns à éviter

- ❌ Ne PAS écrire `pg_dump` avec un mot de passe en clair (utiliser `docker exec` qui hérite de l'env du container)
- ❌ Ne PAS oublier le lock (deux backups concurrents corrompent le dump)
- ❌ Ne PAS faire `set -e` seul — toujours `set -euo pipefail`
- ❌ Ne PAS hardcoder le bucket S3 — env var avec défaut
- ❌ Ne PAS oublier le `--region` sur `aws s3 cp` (peut faire échouer si bucket cross-region)
- ❌ Ne PAS supprimer le dump local AVANT confirmation upload S3 succès

---

## 7. TÂCHE 5 — Préparer `docker/pgbouncer/` config + runbook (S0, 2h)

### Objectif

Le pool DB direct (`db_pool_size=20, db_max_overflow=10` par worker uvicorn) ne tient pas à 1M+ users concurrent (calcul audit D3). PgBouncer en transaction-mode entre uvicorn et Postgres est la solution standard : 10 000 connexions client × 200 connexions Postgres effectives = saturation évitée.

**Cette tâche prépare** :
1. Une config PgBouncer prête à déployer (`pgbouncer.ini` + `userlist.txt` template)
2. Un service Docker dans un nouveau `docker/docker-compose.pgbouncer.yml` (séparé pour test isolé)
3. La modification de `app/core/database/postgres.py` pour ajouter un setting optionnel qui pointe vers PgBouncer
4. La doc `deployment-l2.md` enrichie avec la section PgBouncer
5. Un test local Docker qui valide que la stack fonctionne

**On ne touche PAS** au `docker/docker-compose.yml` dev (PgBouncer reste optionnel en dev). On crée un `docker/docker-compose.pgbouncer.yml` overlay activable par flag `-f`.

### Fichiers à créer/modifier

1. **CRÉER** `docker/pgbouncer/pgbouncer.ini`
2. **CRÉER** `docker/pgbouncer/userlist.txt.example`
3. **CRÉER** `docker/docker-compose.pgbouncer.yml` (overlay)
4. **MODIFIER** `app/config.py` — ajouter setting `database_use_pgbouncer: bool` + commentaire pool sizes
5. **MODIFIER** `app/core/database/postgres.py` — adapter pool si PgBouncer actif (pool externe)
6. **MODIFIER** `.env.example` — documenter `DATABASE_USE_PGBOUNCER` + variants de `DATABASE_URL`
7. **MODIFIER** `docs/runbooks/deployment-l2.md` — section dédiée PgBouncer
8. **CRÉER** `tests/test_pgbouncer_config.py` — vérifications structurelles

### Méthode

#### Étape 5.1 — Lire les fichiers existants

```
Read app/core/database/postgres.py (intégralement)
Read app/config.py (sections "Pool DB", "Database")
Read docker/docker-compose.yml (intégralement)
Read docs/runbooks/deployment-l2.md (sections architecture cible + secrets)
Read .env.example (section database)
```

#### Étape 5.2 — Créer `docker/pgbouncer/pgbouncer.ini`

```ini
;; ════════════════════════════════════════════════════════════════
;; NEXYA PgBouncer config — transaction pooling mode
;; ════════════════════════════════════════════════════════════════
;;
;; Cible : 10 000+ connexions client → ~200 connexions Postgres réelles.
;;
;; Mode `transaction` : chaque transaction utilise n'importe quelle
;; connexion serveur disponible. Le client garde l'illusion d'une
;; connexion persistante. Compatible SQLAlchemy async + psycopg 3.2.
;;
;; ⚠️ Limitations transaction mode (à connaître côté code) :
;; - Pas de session-level statements (SET, LISTEN/NOTIFY persistants)
;; - Prepared statements server-side : nécessite `prepared_statements=false`
;;   côté psycopg OU `pgbouncer.ini server_reset_query = DISCARD ALL`
;; - SQLAlchemy async fonctionne nativement (NEXYA n'utilise pas LISTEN/NOTIFY)
;;
;; Doc officielle : https://www.pgbouncer.org/config.html

[databases]
;; Le client se connecte à `nexya` sur le port PgBouncer (6432)
;; PgBouncer route vers le vrai Postgres sur `postgres:5432`.
nexya = host=postgres port=5432 dbname=nexya pool_size=20 reserve_pool_size=5

;; (V2 si read replica) — pour décharger les SELECT longs
;; nexya_replica = host=postgres-replica port=5432 dbname=nexya pool_size=20

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432

;; ── Auth ──
;; SCRAM-SHA-256 standard moderne. La liste users est dans userlist.txt.
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
auth_user = nexya

;; ── Pool sizing ──
;; Calibré pour Hetzner CCX23 (4 vCPU + 16 GB RAM) en V1 staging.
;; À reconsidérer Phase 14 selon métriques réelles.
;;
;; pool_mode=transaction → connexions serveur partagées entre toutes les
;; transactions client. Mode safe pour SQLAlchemy async.
pool_mode = transaction

;; Total connexions Postgres maximum (cap absolu — Postgres typique max 100-500)
max_client_conn = 10000

;; Connexions serveur par DB (cf. pool_size dans [databases] ci-dessus)
default_pool_size = 20
reserve_pool_size = 5
reserve_pool_timeout = 5

;; ── Timeouts ──
server_idle_timeout = 600
server_lifetime = 3600
query_wait_timeout = 30
client_idle_timeout = 0

;; ── Reset query (transaction mode) ──
;; Nettoie l'état serveur entre 2 transactions client. DISCARD ALL est
;; le défaut sûr pour SQLAlchemy + psycopg.
server_reset_query = DISCARD ALL

;; ── Logging ──
log_connections = 0
log_disconnections = 0
log_pooler_errors = 1
log_stats = 1
stats_period = 60

;; ── Admin ──
;; Liste users autorisés à se connecter sur la DB virtuelle "pgbouncer"
;; pour faire SHOW POOLS / SHOW CLIENTS / RELOAD. NEXYA pose le user
;; admin via env var POSTGRES_USER au boot.
admin_users = nexya
stats_users = nexya

;; ── TLS (optionnel — activé en prod L2) ──
;; client_tls_sslmode = require
;; client_tls_cert_file = /etc/pgbouncer/server.crt
;; client_tls_key_file = /etc/pgbouncer/server.key
;; server_tls_sslmode = require
```

#### Étape 5.3 — Créer `docker/pgbouncer/userlist.txt.example`

```
;; userlist.txt — credentials PgBouncer (SCRAM-SHA-256 hash)
;;
;; Format : "user" "scram_hash"
;;
;; Génération du hash :
;;   1. Connecte-toi au Postgres en superuser
;;   2. SET password_encryption = 'scram-sha-256';
;;   3. ALTER USER nexya WITH PASSWORD 'votre-mot-de-passe-fort';
;;   4. SELECT rolname, rolpassword FROM pg_authid WHERE rolname = 'nexya';
;;   5. Copier la valeur rolpassword ici (commence par "SCRAM-SHA-256$...")
;;
;; ⚠️ Ce fichier ne doit PAS être commit dans git avec de vraies credentials.
;; Le fichier .example sert de template — le vrai userlist.txt est généré
;; au déploiement L2 par le secrets manager (Doppler / 1Password / SSM).

"nexya" "SCRAM-SHA-256$4096:REPLACE_AVEC_LE_VRAI_HASH"
```

#### Étape 5.4 — Créer `docker/docker-compose.pgbouncer.yml`

```yaml
# ══════════════════════════════════════════════════════════════
# NEXYA PgBouncer overlay — test local optionnel
# Usage : docker compose -f docker/docker-compose.yml \
#                        -f docker/docker-compose.pgbouncer.yml up -d
# ══════════════════════════════════════════════════════════════
#
# Cette stack ajoute PgBouncer entre les clients et Postgres pour
# tester localement le routing pool. À NE PAS utiliser en dev quotidien
# (overhead inutile) — réservé aux tests de charge N4 et au smoke test
# avant déploiement L2.

services:
  pgbouncer:
    image: bitnami/pgbouncer:1.23.1
    container_name: nexya-pgbouncer
    ports:
      # 6433 host:6432 container — le 6432 host peut être occupé par
      # un PgBouncer natif Linux. On expose sur 6433 par cohérence
      # avec la convention NEXYA (postgres natif Windows sur 5432,
      # docker postgres sur 5433).
      - "6433:6432"
    volumes:
      - ./pgbouncer/pgbouncer.ini:/bitnami/pgbouncer/conf/pgbouncer.ini:ro
      - ./pgbouncer/userlist.txt:/bitnami/pgbouncer/conf/userlist.txt:ro
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -p 6432 -U nexya || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
    restart: unless-stopped
```

#### Étape 5.5 — Modifier `app/config.py`

Section "Pool DB" (~ligne 596-606), ajouter et **commenter** :

```python
# ── Database pool (direct mode V1) ─────────────────────────────
# IMPORTANT : ces tailles s'appliquent côté SQLAlchemy par worker uvicorn.
# - Sans PgBouncer : 1 worker × pool_size+overflow = 30 connexions Postgres
#   directes. À 1M users concurrent, intenable (cf. audit D3 finding S0).
# - Avec PgBouncer (transaction mode) : SQLAlchemy garde un pool plus large
#   (50+10) côté backend, PgBouncer multiplexe vers ~20 connexions serveur
#   réelles. Cf. `database_use_pgbouncer` ci-dessous.
db_pool_size: int = 20
db_max_overflow: int = 10

# ── PgBouncer routing (préparation L2 staging — non actif en dev) ──
# Quand True, indique que `database_url` pointe vers un PgBouncer en
# transaction mode (port 6432 typique). Le code peut alors :
# 1. Augmenter les pool sizes côté SQLAlchemy (PgBouncer multiplexe)
# 2. Désactiver les prepared statements server-side (`prepared_statement_name_func`)
#    car incompatibles avec le transaction mode PgBouncer
# 3. Désactiver `pool_pre_ping=True` (PgBouncer gère ses propres healthchecks)
#
# Détails : voir docker/pgbouncer/pgbouncer.ini + docs/runbooks/deployment-l2.md
# section "PgBouncer".
database_use_pgbouncer: bool = False
```

#### Étape 5.6 — Modifier `app/core/database/postgres.py`

Adapter la création de l'engine pour gérer le mode PgBouncer :

```python
"""
Connexion PostgreSQL asynchrone — pool de connexions SQLAlchemy.

Deux modes supportés :

1. **Direct** (`database_use_pgbouncer=False`, défaut V1 dev) :
   SQLAlchemy ouvre un pool direct vers Postgres. Pool sizes calibrés
   conservativement (20+10). Bon en dev local et staging < 100k users.

2. **PgBouncer transaction mode** (`database_use_pgbouncer=True`, V1 prod) :
   SQLAlchemy se connecte à PgBouncer (port 6432) qui multiplexe vers
   Postgres. Pool SQLAlchemy plus large possible. Quelques contraintes :
   - `pool_pre_ping=False` (PgBouncer gère)
   - `statement_cache_size=0` côté psycopg (les prepared statements
     server-side ne survivent pas au reset de transaction PgBouncer)
   - `pool_recycle` plus court (la connexion vers PgBouncer ne « voit »
     pas les coupures côté Postgres réel)

get_db() est la dépendance FastAPI injectée dans chaque endpoint qui a
besoin d'accéder à la base de données.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

log = structlog.get_logger()


def _build_engine_kwargs() -> dict:
    """Calcule les kwargs `create_async_engine` selon `database_use_pgbouncer`."""
    if settings.database_use_pgbouncer:
        # Mode PgBouncer transaction : pool SQLAlchemy multiplexé, pas de
        # prepared statements server-side, pas de pre-ping (PgBouncer gère).
        return {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "echo": settings.db_echo,
            "pool_pre_ping": False,
            "pool_recycle": 300,  # 5 min — plus court car PgBouncer masque l'état Postgres
            "connect_args": {
                "connect_timeout": 5,
                # psycopg 3 : désactive prepared statements server-side
                # (incompatible transaction mode PgBouncer)
                "prepare_threshold": None,
            },
        }
    # Mode direct (dev/staging V1)
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "connect_args": {"connect_timeout": 5},
    }


# ── Engine — connexion de bas niveau au pool PostgreSQL ────────
engine = create_async_engine(settings.database_url, **_build_engine_kwargs())

# Log au boot pour visibilité ops
log.info(
    "database.engine_initialized",
    use_pgbouncer=settings.database_use_pgbouncer,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

# ── Session factory ────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dépendance FastAPI — fournit une session DB par requête."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db_connection() -> bool:
    """Vérifie que PostgreSQL est accessible."""
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        log.info("database.connected", url=settings.database_url.split("@")[-1])
        return True
    except Exception as exc:
        log.error("database.connection_failed", error=str(exc))
        return False


async def dispose_engine() -> None:
    """Ferme proprement le pool de connexions."""
    await engine.dispose()
    log.info("database.pool_closed")
```

#### Étape 5.7 — Modifier `.env.example`

Section database, ajouter :

```bash
# ── Database (pool direct V1 dev — PgBouncer L2 staging) ──
DATABASE_URL=postgresql+psycopg://nexya:nexya_dev@localhost:5433/nexya
DATABASE_USE_PGBOUNCER=false
# En L2 staging avec PgBouncer :
# DATABASE_URL=postgresql+psycopg://nexya:<strong-pwd>@pgbouncer:6432/nexya
# DATABASE_USE_PGBOUNCER=true
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=10
```

#### Étape 5.8 — Modifier `docs/runbooks/deployment-l2.md`

Ajouter une nouvelle section après "Architecture cible L2" :

```markdown
---

## PgBouncer (cap connexions à 9M users)

### Pourquoi

Sans PgBouncer, chaque worker uvicorn ouvre `db_pool_size + db_max_overflow`
connexions Postgres directes. À l'échelle :
- 1000 workers × 30 connexions = 30 000 connexions Postgres
- Postgres typique plafonne 100-500 connexions/instance
- Saturation immédiate à 100k users concurrent

PgBouncer en transaction mode multiplexe :
- 10 000 clients × 200 connexions Postgres effectives = ratio 50:1

### Stack

```
uvicorn → PgBouncer (port 6432, transaction mode) → Postgres (port 5432)
```

### Config livrée (2026-05-01)

- `docker/pgbouncer/pgbouncer.ini` — config transaction mode, pool_size=20,
  max_client_conn=10 000, scram-sha-256 auth, server_reset_query=DISCARD ALL
- `docker/pgbouncer/userlist.txt.example` — template avec doc génération
  hash SCRAM
- `docker/docker-compose.pgbouncer.yml` — overlay docker-compose pour test
  local

### Activation L2 staging

1. Sur Hetzner staging, démarrer PgBouncer aux côtés de Postgres :
   ```bash
   docker compose -f docker/docker-compose.yml \
                  -f docker/docker-compose.pgbouncer.yml \
                  up -d
   ```

2. Générer le hash SCRAM-SHA-256 du user `nexya` :
   ```bash
   docker exec -it nexya-postgres psql -U nexya -c \
     "SELECT rolname, rolpassword FROM pg_authid WHERE rolname='nexya';"
   ```
   Copier la valeur `rolpassword` dans `docker/pgbouncer/userlist.txt`
   (PAS dans `userlist.txt.example` qui reste un template).

3. Mettre à jour `.env.production` :
   ```bash
   DATABASE_URL=postgresql+psycopg://nexya:<pwd>@pgbouncer:6432/nexya
   DATABASE_USE_PGBOUNCER=true
   ```

4. Smoke test :
   ```bash
   docker compose ... exec backend python -c \
     "import asyncio; from app.core.database.postgres import check_db_connection; \
     print(asyncio.run(check_db_connection()))"
   # Doit afficher True
   ```

5. Vérifier les pools depuis PgBouncer :
   ```bash
   docker exec -it nexya-pgbouncer psql -U nexya -p 6432 pgbouncer -c "SHOW POOLS;"
   ```

### Limitations connues

- **Pas de LISTEN/NOTIFY persistant** — NEXYA n'en utilise pas (vérifié par
  grep).
- **Pas de prepared statements server-side** — désactivés via
  `prepare_threshold=None` côté psycopg (cf. `app/core/database/postgres.py`).
- **Sessions courtes obligatoires** — chaque transaction libère sa connexion
  serveur. Compatible SQLAlchemy async.

### Monitoring

- Métriques Prometheus à exposer V2 : `pgbouncer_pool_max`, `pgbouncer_clients_active`,
  `pgbouncer_clients_waiting` (via `pgbouncer_exporter`).
- Alerte K2 à ajouter Phase 14 : `NexyaPgBouncerSaturation` (clients_waiting >
  0 sur 5 min).
```

#### Étape 5.9 — Tests structure

Créer `tests/test_pgbouncer_config.py` :

```python
"""Tests structurels de la config PgBouncer.

Ne lance PAS PgBouncer réel — vérifie uniquement la cohérence des
fichiers de config + le comportement code côté `app/core/database/postgres.py`.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
PGBOUNCER_INI = ROOT / "docker" / "pgbouncer" / "pgbouncer.ini"
USERLIST_EXAMPLE = ROOT / "docker" / "pgbouncer" / "userlist.txt.example"
COMPOSE_OVERLAY = ROOT / "docker" / "docker-compose.pgbouncer.yml"


def test_pgbouncer_ini_exists():
    assert PGBOUNCER_INI.exists()


def test_pgbouncer_ini_uses_transaction_mode():
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert re.search(r"^pool_mode\s*=\s*transaction", content, re.M)


def test_pgbouncer_ini_uses_scram_sha256():
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert re.search(r"^auth_type\s*=\s*scram-sha-256", content, re.M)


def test_pgbouncer_ini_uses_discard_all_reset():
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert "DISCARD ALL" in content


def test_pgbouncer_ini_listens_on_6432():
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    assert re.search(r"^listen_port\s*=\s*6432", content, re.M)


def test_pgbouncer_ini_max_client_conn_reasonable():
    content = PGBOUNCER_INI.read_text(encoding="utf-8")
    m = re.search(r"^max_client_conn\s*=\s*(\d+)", content, re.M)
    assert m, "max_client_conn doit être défini"
    value = int(m.group(1))
    assert 1000 <= value <= 100_000, f"max_client_conn={value} hors range raisonnable"


def test_userlist_example_exists():
    assert USERLIST_EXAMPLE.exists()


def test_userlist_example_documents_scram_generation():
    content = USERLIST_EXAMPLE.read_text(encoding="utf-8")
    assert "SCRAM-SHA-256" in content
    assert "scram-sha-256" in content.lower() or "password_encryption" in content


def test_userlist_example_no_real_credentials():
    content = USERLIST_EXAMPLE.read_text(encoding="utf-8")
    # Le template doit utiliser un placeholder évident
    assert "REPLACE" in content or "EXAMPLE" in content.upper() or "PLACEHOLDER" in content.upper()


def test_compose_overlay_exists():
    assert COMPOSE_OVERLAY.exists()


def test_compose_overlay_is_valid_yaml():
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert "services" in data
    assert "pgbouncer" in data["services"]


def test_compose_overlay_pinned_image():
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    image = data["services"]["pgbouncer"]["image"]
    assert ":" in image, "Image doit être pinned (pas :latest)"
    assert image.split(":")[1] not in ("latest", "main", "master")


def test_compose_overlay_mounts_config_readonly():
    with COMPOSE_OVERLAY.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    volumes = data["services"]["pgbouncer"]["volumes"]
    assert any("pgbouncer.ini" in v and ":ro" in v for v in volumes)
    assert any("userlist.txt" in v and ":ro" in v for v in volumes)


def test_postgres_engine_supports_pgbouncer_mode():
    """Le code postgres.py doit ajuster les kwargs en mode PgBouncer."""
    src = (ROOT / "app" / "core" / "database" / "postgres.py").read_text(encoding="utf-8")
    assert "database_use_pgbouncer" in src
    assert "pool_pre_ping" in src
    assert "prepare_threshold" in src


def test_settings_has_pgbouncer_flag():
    """settings.database_use_pgbouncer doit être défini."""
    from app.config import Settings
    s = Settings()  # ne devrait pas exiger ENV vars critiques
    assert hasattr(s, "database_use_pgbouncer")
    # Défaut V1 = False (pas de PgBouncer en dev)
    assert s.database_use_pgbouncer is False


def test_deployment_l2_runbook_documents_pgbouncer():
    """deployment-l2.md doit documenter la procédure PgBouncer."""
    content = (ROOT / "docs" / "runbooks" / "deployment-l2.md").read_text(encoding="utf-8")
    assert "PgBouncer" in content
    assert "transaction mode" in content.lower()
    assert "userlist.txt" in content
```

#### Étape 5.10 — Test manuel local (optionnel)

```bash
# Démarrer la stack avec PgBouncer
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.pgbouncer.yml up -d

# Vérifier que pgbouncer est healthy
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.pgbouncer.yml ps pgbouncer
# STATUS doit être "healthy"

# Générer le hash SCRAM réel pour le user nexya
docker exec -it nexya-postgres psql -U nexya -c \
  "SELECT rolpassword FROM pg_authid WHERE rolname='nexya';"
# Copier le résultat dans docker/pgbouncer/userlist.txt (créer le fichier)

# Tester la connexion via PgBouncer (port 6433 host)
docker exec -it nexya-pgbouncer psql -U nexya -p 6432 nexya -c "SELECT 1;"

# Cleanup
docker compose -f docker/docker-compose.yml \
               -f docker/docker-compose.pgbouncer.yml down
```

Si le hash SCRAM pose problème en local, c'est OK — le but est de valider que la stack démarre, pas que la connexion fonctionne (le hash réel sera généré au déploiement L2).

### Critères d'acceptation tâche 5

- [ ] `docker/pgbouncer/pgbouncer.ini` créé, transaction mode, scram-sha-256, max_client_conn=10000, server_reset_query=DISCARD ALL
- [ ] `docker/pgbouncer/userlist.txt.example` créé avec doc génération hash + placeholder évident
- [ ] `docker/docker-compose.pgbouncer.yml` créé, image pinned, mounts read-only, healthcheck pg_isready
- [ ] `app/config.py` ajoute `database_use_pgbouncer: bool = False` avec docstring
- [ ] `app/core/database/postgres.py` adapte les kwargs engine selon le flag
- [ ] `.env.example` ajoute `DATABASE_USE_PGBOUNCER=false` + commentaire L2
- [ ] `docs/runbooks/deployment-l2.md` ajoute section PgBouncer complète (pourquoi, stack, activation, limitations, monitoring)
- [ ] `tests/test_pgbouncer_config.py` créé avec ~16 tests structurels verts
- [ ] Suite globale 1583 tests : 0 régression
- [ ] Stack docker-compose démarre proprement en local (test manuel optionnel)

### Anti-patterns à éviter

- ❌ Ne PAS modifier `docker/docker-compose.yml` dev — PgBouncer reste optionnel via overlay
- ❌ Ne PAS hardcoder un mot de passe dans `pgbouncer.ini` — utiliser `userlist.txt` séparé
- ❌ Ne PAS commit `userlist.txt` réel — uniquement `.example`. Ajouter `userlist.txt` au `.gitignore` si pas déjà fait.
- ❌ Ne PAS mettre `pool_mode = session` — incompatible avec l'objectif de scaling (transaction mode obligatoire)
- ❌ Ne PAS oublier `server_reset_query = DISCARD ALL` — sans ça, des paramètres de session fuitent entre transactions client
- ❌ Ne PAS oublier `pool_pre_ping=False` côté SQLAlchemy quand PgBouncer actif — sinon double healthcheck inutile

---

## 8. Validation finale (avant commit)

Après les 5 tâches :

### 8.1 — Suite de tests complète

```bash
pytest tests/ -p no:warnings -q
```

Attendu : **1583 tests existants + ~25 nouveaux tests = ~1608 verts, 0 failed, 0 errors**.

Si une régression apparaît : **stop**, fix avant de continuer.

### 8.2 — Lint + format

```bash
ruff check app/ tests/ scripts/
ruff format --check app/ tests/
bash -n scripts/backup_db.sh
bash -n scripts/restore_db.sh
```

Tout doit passer.

### 8.3 — Mise à jour des docs (règle `feedback_docs_update.md` BLOQUANTE)

**Obligatoire** avant annonce de fin :

1. **`CLAUDE.md` §15** — ajouter une entrée datée 2026-05-01 résumant les 5 tâches livrées (3 fixes code + 2 préparations infra), avec liste des fichiers impactés.

2. **`CLAUDE.md` §7** — mettre à jour les colonnes Statut si pertinent (ex: la ligne « Headers sécurité » reste ✅, mais on peut ajouter une mention « PgBouncer config livrée 2026-05-01, activation L2 staging » dans une nouvelle ligne d'infra prod).

3. **`docs/ROADMAP.md`** — marquer P0 "/auth/refresh rate limit" + "max_tokens cap" + "blacklist alert" comme terminés. Marquer "PgBouncer config" et "backup script" comme "préparés (déploiement L2)".

4. **`docs/BACKEND_SESSIONS_PLAN.md`** — ajouter une session « Audit Remediation 2026-05-01 » dans le bloc le plus pertinent.

5. **`COURS_NEXYA_BACKEND.md`** — ajouter une mini-section pédagogique :
   - Pourquoi un rate limit IP sur /auth/refresh ?
   - Pourquoi cap max_tokens ?
   - Pourquoi fail-open sur blacklist + métrique Prometheus ?
   - Pourquoi PgBouncer en transaction mode ?
   - Pourquoi pg_dump --format=custom + sse aes256 ?

### 8.4 — Commit final

Un seul commit, message **sans** Conventional Commits, **sans** Co-Authored-By, ~10 lignes max décrivant les 5 tâches :

```
Audit remediation 2026-05-01 — 5 tâches priorisées

3 fixes code :
- Rate limit IP /auth/refresh (20/min) — S0 brute-force JWT
- Cap max_tokens explicite sur 11 experts — S1 facture runaway
- Blacklist JWT fail-open + métrique Prometheus — S1 Redis down

2 préparations infra (déploiement L2) :
- scripts/backup_db.sh + restore_db.sh + tests structure
- docker/pgbouncer/ config + overlay docker-compose + tests

~25 nouveaux tests verts, 0 régression sur 1583 existants.
Suite finale : ~1608 tests passed.
```

### 8.5 — Annonce finale à Ivan

Après commit, message d'annonce respectant `feedback_docs_update.md` :

```
✅ Audit remediation 2026-05-01 livrée. 5 tâches en 7h.

Récap :
- T1 /auth/refresh rate limit : S0 fermé
- T2 max_tokens cap : S1 fermé (facture protégée)
- T3 blacklist alert : S1 fermé (observabilité enrichie)
- T4 backup_db.sh : préparé, sera activé au déploiement L2
- T5 PgBouncer config : préparé, sera activé au déploiement L2

Tests : N+25 verts, 0 régression.
Docs : §15 + ROADMAP + COURS + BACKEND_SESSIONS_PLAN à jour.

Prochaine étape : engager consultant DPO pour DPIA (lead time 4-6 semaines).
```

---

## 9. Récapitulatif des fichiers créés / modifiés

### Créés (8)
1. `scripts/backup_db.sh`
2. `scripts/restore_db.sh`
3. `docker/pgbouncer/pgbouncer.ini`
4. `docker/pgbouncer/userlist.txt.example`
5. `docker/docker-compose.pgbouncer.yml`
6. `tests/test_backup_scripts.py`
7. `tests/test_pgbouncer_config.py`
8. (entrée §15 CLAUDE.md datée 2026-05-01)

### Modifiés (10)
1. `app/core/security/rate_limiter.py`
2. `app/features/auth/router.py`
3. `app/ai/experts.py`
4. `app/core/auth/jwt.py`
5. `app/core/observability/prometheus.py`
6. `app/core/database/postgres.py`
7. `app/config.py`
8. `.env.example`
9. `tests/test_auth_hardening.py`
10. `tests/test_experts_registry.py`

### Docs modifiées (5)
1. `CLAUDE.md` (§7, §15)
2. `docs/ROADMAP.md`
3. `docs/BACKEND_SESSIONS_PLAN.md`
4. `COURS_NEXYA_BACKEND.md`
5. `docs/runbooks/db-restore.md`
6. `docs/runbooks/deployment-l2.md`

---

## 10. Hors-scope strict (à ne PAS faire dans cette session)

- ❌ Déployer quoi que ce soit sur Hetzner ou autre serveur prod
- ❌ Toucher aux comptes externes (S3 réel, Cloudflare DNS, GitHub secrets)
- ❌ Lancer des tests de charge k6 réels
- ❌ Modifier les workflows GitHub Actions
- ❌ Ajouter de nouveaux endpoints
- ❌ Modifier les schémas DB ou créer de nouvelles migrations
- ❌ Toucher aux 8 autres findings P0/P1 du Top 10 audit (ils attendent leur tour)
- ❌ Refactorer / découper `main.py` ou `chat/router.py` (S2/S3, hors scope)
- ❌ Démarrer le DPIA / contacter consultant DPO (action externe Ivan, hors scope code)
- ❌ Lancer `pip-audit` ou bumper des deps CVE pypdf/pytest

---

*Fin du prompt de remédiation. Ce document est l'instruction maîtresse. Le `TODO_REMEDIATION_BACKEND_NEXYA_2026-05-01.md` détaille chaque vérification atomique attendue.*
