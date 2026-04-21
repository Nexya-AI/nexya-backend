"""
Rate limiter IP + user — sliding window via Redis.

Deux familles de limites coexistent :

1. **IP-scoped** (`check_ip_rate_limit`) : protège les endpoints publics
   non authentifiés contre le brute-force (login, register).

2. **User-scoped** (`check_user_rate_limit`) : protège les actions
   authentifiées sensibles contre l'abus (signalements, par exemple).
   Le compteur est porté par l'`user_id`, pas par l'IP — un même user
   derrière un proxy mobile (NAT carrier) ne peut pas contourner sa
   limite en sautant d'IP.

Limites (CLAUDE.md section 13) :
- Login    : 10 tentatives / minute par IP
- Register : 5 tentatives / minute par IP
- Signalements abus : 10 / heure / utilisateur (anti-spam UI)

Algorithme commun : sliding window counter (Redis INCR + EXPIRE atomique
au premier hit). Simple, performant, suffisant pour notre cas d'usage.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import Request

from app.core.database.redis import get_redis
from app.core.errors.exceptions import RateLimitAbuseException, RateLimitIPException

log = structlog.get_logger()

# ── Préfixes Redis ─────────────────────────────────────────────
RATE_LIMIT_PREFIX = "rate:ip:"
USER_RATE_LIMIT_PREFIX = "rate:user:"


def _get_client_ip(request: Request) -> str:
    """Extrait l'IP du client, en tenant compte des proxies (X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def check_ip_rate_limit(
    request: Request,
    action: str,
    max_requests: int,
    window_seconds: int = 60,
) -> None:
    """Vérifie le rate limit IP pour une action donnée.

    Args:
        request: requête FastAPI (pour extraire l'IP)
        action: identifiant de l'action ('login', 'register')
        max_requests: nombre maximum de requêtes dans la fenêtre
        window_seconds: taille de la fenêtre en secondes (défaut 60s)

    Raises:
        RateLimitIPException: si le seuil est dépassé
    """
    ip = _get_client_ip(request)
    key = f"{RATE_LIMIT_PREFIX}{action}:{ip}"

    redis = get_redis()

    # INCR atomique + TTL au premier appel
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, window_seconds)

    if current > max_requests:
        ttl = await redis.ttl(key)
        log.warning(
            "rate_limit.ip.exceeded",
            action=action,
            ip=ip,
            current=current,
            max=max_requests,
            retry_after=ttl,
        )
        raise RateLimitIPException(retry_after=max(ttl, 1))


async def rate_limit_login(request: Request) -> None:
    """Rate limit pour POST /auth/login — 10 requêtes/minute par IP."""
    await check_ip_rate_limit(request, action="login", max_requests=10)


async def rate_limit_register(request: Request) -> None:
    """Rate limit pour POST /auth/register — 5 requêtes/minute par IP."""
    await check_ip_rate_limit(request, action="register", max_requests=5)


# ══════════════════════════════════════════════════════════════
# RATE LIMIT — user-scoped
# ══════════════════════════════════════════════════════════════

async def check_user_rate_limit(
    user_id: uuid.UUID | str,
    action: str,
    max_requests: int,
    window_seconds: int,
    *,
    on_exceeded: type[Exception] = RateLimitAbuseException,
) -> None:
    """Vérifie le rate limit user pour une action donnée.

    Args:
        user_id: identifiant utilisateur (UUID ou str)
        action: identifiant de l'action ('abuse_report', etc.)
        max_requests: nombre maximum de requêtes dans la fenêtre
        window_seconds: taille de la fenêtre en secondes
        on_exceeded: classe d'exception à lever (doit accepter `retry_after=int`)

    Raises:
        on_exceeded: si le seuil est dépassé (retry_after = TTL de la clé)
    """
    key = f"{USER_RATE_LIMIT_PREFIX}{action}:{user_id}"
    redis = get_redis()

    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, window_seconds)

    if current > max_requests:
        ttl = await redis.ttl(key)
        log.warning(
            "rate_limit.user.exceeded",
            action=action,
            user_id=str(user_id),
            current=current,
            max=max_requests,
            retry_after=ttl,
        )
        raise on_exceeded(retry_after=max(ttl, 1))


async def rate_limit_abuse_reports(user_id: uuid.UUID | str) -> None:
    """Rate limit pour POST /chat/reports — 10 signalements/heure/utilisateur.

    Code dédié (`RATE_LIMIT_ABUSE`) plutôt que `RATE_LIMIT_IP` : le Flutter
    distingue un IP pénalisée (brute-force auth) d'un user qui spamme
    le bouton « Signaler » (mécanisme anti-abus du modération).
    """
    await check_user_rate_limit(
        user_id,
        action="abuse_report",
        max_requests=10,
        window_seconds=3600,
    )
