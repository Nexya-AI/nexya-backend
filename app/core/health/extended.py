"""
Service Health Étendu — `/ready` enrichi (Session O1 volet B).

Aggrège des indicateurs ops critiques :
- `version` : commit_sha[:8] + tag git + dirty flag (cf. version.py)
- `db` : statut + latence ms (`SELECT 1`)
- `redis` : statut + latence ms (`PING`)
- `arq_queue_depth` : profondeur de la queue arq
  (`ZCARD arq:queue` — arq utilise un sorted set par défaut)
- `last_migration` : `version_num` Alembic appliqué actuellement
- `uptime_seconds` : depuis le lifespan startup

**Tous les fields sont fail-safe** : exception → field=None + log warning,
jamais cascade. Le caller (endpoint `/ready`) décide si globalement
`status='ok'` ou `'degraded'` selon que `db` et `redis` répondent.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.health.version import detect_version

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# UPTIME GLOBAL — posé au lifespan startup
# ═══════════════════════════════════════════════════════════════════


_APP_START_MONOTONIC: float | None = None


def set_app_start_monotonic(value: float | None = None) -> None:
    """Pose le timestamp monotonic de boot. Appelé au lifespan `startup`."""
    global _APP_START_MONOTONIC
    _APP_START_MONOTONIC = value if value is not None else time.monotonic()


def get_app_start_monotonic() -> float | None:
    return _APP_START_MONOTONIC


def _compute_uptime_seconds() -> float | None:
    if _APP_START_MONOTONIC is None:
        return None
    return round(time.monotonic() - _APP_START_MONOTONIC, 3)


# ═══════════════════════════════════════════════════════════════════
# RESPONSE SCHEMA
# ═══════════════════════════════════════════════════════════════════


class _VersionPayload(BaseModel):
    version: str
    commit_sha: str
    tag: str | None = None
    dirty: bool
    source: str


class _DbPayload(BaseModel):
    status: str  # "ok" | "unavailable"
    latency_ms: float | None = None
    last_migration: str | None = None


class _RedisPayload(BaseModel):
    status: str  # "ok" | "unavailable"
    latency_ms: float | None = None


class _ArqPayload(BaseModel):
    queue_depth: int | None = None


class ExtendedHealthResponse(BaseModel):
    """Réponse complète de `/ready` post-O1."""

    status: str  # "ok" | "degraded"
    version: _VersionPayload
    db: _DbPayload
    redis: _RedisPayload
    arq: _ArqPayload
    uptime_seconds: float | None = None


# ═══════════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════════


class ExtendedHealthService:
    """Compute la `ExtendedHealthResponse` complète + fail-safe par champ."""

    @staticmethod
    async def compute(
        *,
        db: AsyncSession | None,
        redis: Any | None,
    ) -> ExtendedHealthResponse:
        version = _build_version_payload()
        db_payload = await _build_db_payload(db)
        redis_payload = await _build_redis_payload(redis)
        arq_payload = await _build_arq_payload(redis)
        uptime = _compute_uptime_seconds()

        # Status global : ok si DB ET Redis up.
        ok = db_payload.status == "ok" and redis_payload.status == "ok"
        return ExtendedHealthResponse(
            status="ok" if ok else "degraded",
            version=version,
            db=db_payload,
            redis=redis_payload,
            arq=arq_payload,
            uptime_seconds=uptime,
        )


# ═══════════════════════════════════════════════════════════════════
# BUILDERS — fail-safe par champ
# ═══════════════════════════════════════════════════════════════════


def _build_version_payload() -> _VersionPayload:
    info = detect_version()
    return _VersionPayload(
        version=info.version,
        commit_sha=info.commit_sha,
        tag=info.tag,
        dirty=info.dirty,
        source=info.source,
    )


async def _build_db_payload(db: AsyncSession | None) -> _DbPayload:
    if db is None:
        return _DbPayload(status="unavailable")
    try:
        start = time.perf_counter()
        await db.execute(text("SELECT 1"))
        latency = round((time.perf_counter() - start) * 1000, 2)
    except Exception as exc:  # noqa: BLE001 — fail-safe
        log.debug("health.db_ping_failed", error=str(exc))
        return _DbPayload(status="unavailable")

    last_migration = await _read_last_migration(db)
    return _DbPayload(
        status="ok",
        latency_ms=latency,
        last_migration=last_migration,
    )


async def _read_last_migration(db: AsyncSession) -> str | None:
    """Lit `alembic_version.version_num`. Fail-safe → None."""
    try:
        result = await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        row = result.scalar_one_or_none()
        return str(row) if row else None
    except Exception as exc:  # noqa: BLE001 — fail-safe
        log.debug("health.last_migration_failed", error=str(exc))
        return None


async def _build_redis_payload(redis: Any | None) -> _RedisPayload:
    if redis is None:
        return _RedisPayload(status="unavailable")
    try:
        start = time.perf_counter()
        await redis.ping()
        latency = round((time.perf_counter() - start) * 1000, 2)
        return _RedisPayload(status="ok", latency_ms=latency)
    except Exception as exc:  # noqa: BLE001 — fail-safe
        log.debug("health.redis_ping_failed", error=str(exc))
        return _RedisPayload(status="unavailable")


async def _build_arq_payload(redis: Any | None) -> _ArqPayload:
    """`arq` utilise un sorted set `arq:queue` par défaut.

    `ZCARD` est O(1) côté Redis, donc rapide même avec une grosse queue.
    Best-effort : si la queue n'existe pas (pas de worker démarré), on
    retourne 0 (clé absente = ZCARD retourne 0). Si Redis indispo, on
    retourne None.
    """
    if redis is None:
        return _ArqPayload(queue_depth=None)
    try:
        depth = await redis.zcard("arq:queue")
        return _ArqPayload(queue_depth=int(depth or 0))
    except Exception as exc:  # noqa: BLE001 — fail-safe
        log.debug("health.arq_zcard_failed", error=str(exc))
        return _ArqPayload(queue_depth=None)
