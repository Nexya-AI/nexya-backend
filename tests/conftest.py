"""
Fixtures pytest — couvre la suite P0.

Ces tests sont volontairement **sans base de données** : ils valident la
discipline sécurité (scrubber, CORS, JWT, health checks) sans exiger
qu'un Postgres tourne. Les tests d'intégration DB arriveront avec la
Feature Chat (ils utiliseront `testcontainers` ou une DB de test dédiée).

Pour l'exécution :
    pytest tests/ -v
"""

from __future__ import annotations

import os

import pytest

# Variables minimales pour que `app.config.Settings()` se charge en mode dev
# même si aucun .env n'est présent (ex: CI sans secrets).
os.environ.setdefault("ENV", "development")
os.environ.setdefault("APP_SECRET", "test-secret-not-for-prod")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:65530/test")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:65531/0")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Par défaut, pytest-asyncio tourne sur asyncio — explicit is better."""
    return "asyncio"


# ══════════════════════════════════════════════════════════════
# Isolation des compteurs de rate-limit chat (Redis) entre tests
# ══════════════════════════════════════════════════════════════
#
# Depuis la refonte quotas (2026-06-24), `/chat/stream` applique deux
# compteurs Redis portés par l'user_id : l'anti-bot (`chat_msg`, 100/min)
# et le quota texte Free (`chat_message`, 30 / fenêtre 3h).
#
# En CI un vrai Redis tourne (service `redis:7-alpine`). Les tests
# d'intégration `/chat/stream` réutilisent un même fake user_id constant
# d'un fichier à l'autre — sans purge, le compteur s'accumulerait à
# travers toute la session et les tests tardifs finiraient par recevoir
# un 402 (CHAT_MESSAGE_QUOTA_EXCEEDED) parasite, sans rapport avec ce
# qu'ils valident (wiring pills / tools / contexte).
#
# On purge donc ces deux familles de clés AVANT chaque test. En dev local
# sans Redis, le client échoue vite (timeout) → `_sync_redis` vaut None →
# la purge est un no-op transparent (le code applicatif reste fail-open).


@pytest.fixture(scope="session")
def _sync_redis():
    """Client Redis synchrone réservé aux tests (purge d'isolation).

    Retourne None si Redis est injoignable (dev local) afin que les
    fixtures dépendantes deviennent de simples no-op.
    """
    try:
        import redis as _redis

        from app.config import settings

        client = _redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        client.ping()
        return client
    except Exception:
        return None


@pytest.fixture(autouse=True)
def _reset_chat_rate_limits(_sync_redis):
    """Purge les compteurs de rate-limit chat avant chaque test (isolation CI)."""
    if _sync_redis is not None:
        try:
            for pattern in ("rate:user:chat_message:*", "rate:user:chat_msg:*"):
                keys = list(_sync_redis.scan_iter(match=pattern, count=500))
                if keys:
                    _sync_redis.delete(*keys)
        except Exception:
            pass
    yield
