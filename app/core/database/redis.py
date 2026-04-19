"""
Connexion Redis asynchrone — pool de connexions.

Redis est utilisé pour : sessions JWT, cache, rate limiting, annulation SSE.
"""

from __future__ import annotations

import redis.asyncio as aioredis
import structlog

from app.config import settings

log = structlog.get_logger()

# ── Pool de connexions Redis ───────────────────────────────────
redis_pool = aioredis.ConnectionPool.from_url(
    settings.redis_url,
    max_connections=settings.redis_max_connections,
    decode_responses=True,     # retourne des str Python, pas des bytes
    socket_connect_timeout=3,  # timeout 3s (évite de bloquer si Redis est down)
    socket_timeout=3,
)


def get_redis() -> aioredis.Redis:
    """Retourne un client Redis connecté au pool partagé.

    Pas besoin de async generator ici — le pool gère les connexions.
    Le client est léger, il réutilise les connexions du pool.
    """
    return aioredis.Redis(connection_pool=redis_pool)


async def check_redis_connection() -> bool:
    """Vérifie que Redis est accessible. Utilisé au démarrage (lifespan)."""
    try:
        client = get_redis()
        await client.ping()
        log.info("redis.connected", url=settings.redis_url.split("@")[-1])
        return True
    except Exception as exc:
        log.error("redis.connection_failed", error=str(exc))
        return False


async def close_redis_pool() -> None:
    """Ferme proprement le pool Redis. Appelé à l'arrêt de l'API."""
    await redis_pool.aclose()
    log.info("redis.pool_closed")
