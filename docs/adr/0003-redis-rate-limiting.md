# ADR 0003 — Rate limiting Redis sliding window

## Status

Accepted (2026-04-18)

## Context

NEXYA expose des endpoints sensibles (auth, chat IA, upload) qui
doivent être protégés contre :
- **DDoS** simple (1 IP qui spamme)
- **Brute force** auth (login/register/forgot-password)
- **Abus quotas** (Free user qui tente de dépasser ses 50 chats/jour)
- **Escalation** (5/jour/IP slow & low)

Choix entre :
1. Redis sliding window (INCR + EXPIRE atomique)
2. Postgres-based (table `rate_limit_attempts`)
3. Leaky bucket in-memory (per process)
4. Cloudflare WAF rules

## Decision

**Redis sliding window** — avec INCR + EXPIRE atomique au premier hit.

## Consequences

### Positives

- **Performance** : O(1) Redis, < 1 ms par check
- **Atomicité** : INCR + EXPIRE atomique (pas de race condition)
- **Multi-instance safe** : Redis partagé entre tous les pods backend
- **Reset automatique** : EXPIRE TTL nettoie auto (pas de cron)
- **Pattern flexible** : `rate:user:{action}:{uid}` ou `rate:ip:{action}:{ip}`
  ou `rate:device:{action}:{device_id}` selon scope
- **Implémenté générique** dans `app/core/security/rate_limiter.py` :
  ```python
  await check_user_rate_limit(
      user_id, action="abuse_report",
      max_requests=10, window_seconds=3600,
      on_exceeded=RateLimitAbuseException(retry_after=3600)
  )
  ```

### Négatives

- **Dépendance Redis** : si Redis down, tous les rate limits sautent
- **Pas de quota précis** : sliding window approximatif (vs leaky
  bucket exact)
- **Memory Redis** : 1 user × 5 actions × 1k users actifs/h ~50k clés
  TTL 1h (acceptable, ~10 MB)

### Mitigations

- **Fail-open** sur Redis error (mieux vaut servir un user que
  bloquer tout le monde si Redis flap) — log warning
- **Multiples actions distinctes** par endpoint (`auth_login` ≠
  `auth_register` ≠ `auth_forgot`) pour éviter qu'un quota cross-pollue
- **Alertes K2** : Grafana dashboard alerte si rate limits trigger
  > 100/min (signal d'attaque)

## Implémentation

### Helper générique `check_user_rate_limit`

```python
async def check_user_rate_limit(
    user_id: UUID, action: str,
    *, max_requests: int, window_seconds: int,
    on_exceeded: NexYaException,
) -> None:
    redis = get_redis()
    key = f"rate:user:{action}:{user_id}"
    try:
        current = await redis.incr(key)
        if current == 1:
            await redis.expire(key, window_seconds)
        if current > max_requests:
            raise on_exceeded
    except RedisError:
        log.warning("rate_limit.redis_error", action=action)
        return  # fail-open
```

### Usages NEXYA

- `/auth/login` : 10/min/IP
- `/auth/register` : 5/min/IP + 5/jour/IP + 5/jour/device + captcha
- `/auth/forgot-password` : 10/h/IP + 3/h/email (sentinelle silent
  anti-enumeration)
- `/auth/reset-password` : 5/h/IP
- `/chat/reports` : 10/h/user
- `/files/upload` : 20/h/user
- `/suggestions` : 5/jour/user
- `/vision/analyze` : 30/h/user
- `/voice/transcribe` : 30/h/user
- `/voice/speak` : 60/h/user
- `/rgpd/user/data-export` : 1/24h/user
- `/rag/query` : 60/h/user
- `/notifications/unsubscribe/*` : 10/h/IP
- `/abuse_report` : 10/h/user

## Alternatives considérées

### Postgres-based

**Pour** : pas de dépendance Redis, persistence garantie.

**Contre** :
- Latence ~10-20 ms (vs 1 ms Redis) — impact UX 2G/3G
- Surcharge DB = risque cascade panne
- Cleanup cron complexe (vs EXPIRE TTL Redis auto)

### Leaky bucket in-memory

**Pour** : ultra-rapide, pas de dépendance externe.

**Contre** :
- Pas multi-instance — chaque pod a son compteur, attaquant peut
  spammer N×limit avec N pods
- Reset au restart pod = bypass

### Cloudflare WAF

**Pour** : edge-rate-limiting, pas de charge backend.

**Contre** :
- Coût supplémentaire ($5/M requêtes Cloudflare Pro)
- Granularité limitée (IP only, pas user/device)
- Pas de retour `retry_after` structuré
- **Complémentaire** : on utilise les deux niveaux. Cloudflare WAF V2
  pour DDoS volumétrique, Redis pour les quotas user-scope fins.

## Notes

V2 : envisager **Lua script Redis** pour combiner INCR+EXPIRE en
1 round-trip (vs 2 actuellement) → ~2× perf. Pas critique V1.

V3 : envisager **leaky bucket Redis** via `CL.THROTTLE` (RedisStack)
pour quota précis. V1 sliding window suffit.
