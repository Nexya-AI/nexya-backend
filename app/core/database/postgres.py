"""
Connexion PostgreSQL asynchrone — pool de connexions SQLAlchemy.

get_db() est la dépendance FastAPI injectée dans chaque endpoint
qui a besoin d'accéder à la base de données.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

log = structlog.get_logger()

# ── Engine — connexion de bas niveau au pool PostgreSQL ────────
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    echo=settings.db_echo,
    pool_pre_ping=True,        # vérifie que la connexion est vivante avant de l'utiliser
    pool_recycle=3600,         # recycle les connexions après 1h (évite les timeout réseau)
    connect_args={"connect_timeout": 5},  # timeout 5s sur la connexion TCP (option psycopg v3)
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
