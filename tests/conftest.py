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
