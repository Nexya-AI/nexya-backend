"""
Alembic env.py — mode async pour SQLAlchemy 2.0.

Utilise l'engine async de notre config pour appliquer les migrations.
Importe tous les modèles ORM via Base.metadata pour l'autogenerate.
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig

# Windows : forcer SelectorEventLoop (ProactorEventLoop buggé avec asyncpg sur Py 3.14+)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.core.database.base import Base

# ── Importer TOUS les modèles ORM ici pour que Base.metadata les connaisse ──
# Sans ces imports, autogenerate ne détecte aucune table.
from app.features.auth.models import DeviceToken, RefreshToken, User  # noqa: F401

config = context.config

# Injecter l'URL de la DB depuis notre config (pas depuis alembic.ini)
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Mode offline — génère le SQL sans se connecter à la DB."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Exécute les migrations dans une connexion synchrone."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Mode online async — crée un engine async, exécute les migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
