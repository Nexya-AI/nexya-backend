"""
Rate limiter IP — sliding window via Redis.

Protège les endpoints d'authentification contre le brute-force.
Chaque IP a un compteur glissant : si le nombre de requêtes dépasse
le seuil dans la fenêtre de temps, la requête est rejetée avec 429.

Limites (CLAUDE.md section 13) :
- Login  : 10 tentatives / minute par IP
- Register : 5 tentatives / minute par IP

Algorithme : sliding window counter (Redis INCR + EXPIRE).
- Simple, performant, et suffisant pour notre cas d'usage.
- Pas de dépendance externe (Lua script ou module Redis).
"""

from __future__ import annotations

import structlog
from fastapi import Request

from app.core.database.redis import get_redis
from app.core.errors.exceptions import RateLimitIPException

log = structlog.get_logger()

# ── Préfixe Redis ──────────────────────────────────────────────
RATE_LIMIT_PREFIX = "rate:ip:"


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
