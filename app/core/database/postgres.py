"""
Connexion PostgreSQL asynchrone — pool de connexions SQLAlchemy.

Deux modes supportés (audit 2026-05-01 finding S0 D3) :

1. **Direct** (`database_use_pgbouncer=False`, défaut V1 dev) :
   SQLAlchemy ouvre un pool direct vers Postgres. Pool sizes calibrés
   conservativement (20+10). Bon en dev local et staging < 100k users.

2. **PgBouncer transaction mode** (`database_use_pgbouncer=True`, V1 prod) :
   SQLAlchemy se connecte à PgBouncer (port 6432) qui multiplexe vers
   Postgres. Quelques contraintes critiques :
   - `pool_pre_ping=False` (PgBouncer gère ses propres healthchecks ;
     un double pre-ping ajouterait latence sans bénéfice)
   - `prepare_threshold=None` côté psycopg 3 (les prepared statements
     server-side ne survivent pas au reset de transaction PgBouncer
     `DISCARD ALL` — ils seraient préparés puis perdus à chaque tour)
   - `pool_recycle=300` (5 min — la connexion vers PgBouncer ne « voit »
     pas les coupures côté Postgres réel, recycle plus serré)

`get_db()` est la dépendance FastAPI injectée dans chaque endpoint qui a
besoin d'accéder à la base de données.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

log = structlog.get_logger()


def _build_engine_kwargs() -> dict[str, Any]:
    """Calcule les kwargs `create_async_engine` selon `database_use_pgbouncer`.

    En mode PgBouncer (transaction pooling) :
    - Pas de `pool_pre_ping` (PgBouncer route déjà vers une connexion saine)
    - `pool_recycle=300` plus court (PgBouncer masque les coupures Postgres)
    - `prepare_threshold=None` côté psycopg 3 (anti incompatibilité prepared
      statements server-side)

    En mode direct (V1 dev) : pool_pre_ping=True + recycle 1h standard.
    """
    if settings.database_use_pgbouncer:
        return {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "echo": settings.db_echo,
            "pool_pre_ping": False,
            "pool_recycle": 300,
            "connect_args": {
                "connect_timeout": 5,
                # psycopg 3 : désactive prepared statements server-side
                # (incompatibles avec le transaction mode PgBouncer).
                "prepare_threshold": None,
            },
        }
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

# Log au boot pour visibilité ops (utile pour confirmer le mode actif
# dans les logs au démarrage staging/prod)
log.info(
    "database.engine_initialized",
    use_pgbouncer=settings.database_use_pgbouncer,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

# ── Session factory — crée une session par requête HTTP ────────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,    # les objets restent utilisables après commit
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dépendance FastAPI — fournit une session DB par requête.

    - Commit automatique si pas d'erreur
    - Rollback automatique si exception
    - La session est fermée dans tous les cas (finally implicite via async with)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_db_connection() -> bool:
    """Vérifie que PostgreSQL est accessible. Utilisé au démarrage (lifespan)."""
    try:
        async with engine.connect() as conn:
            await conn.execute(
                # text() importé ici pour éviter un import top-level inutile
                __import__("sqlalchemy").text("SELECT 1")
            )
        log.info("database.connected", url=settings.database_url.split("@")[-1])
        return True
    except Exception as exc:
        log.error("database.connection_failed", error=str(exc))
        return False


async def dispose_engine() -> None:
    """Ferme proprement le pool de connexions. Appelé à l'arrêt de l'API."""
    await engine.dispose()
    log.info("database.pool_closed")
