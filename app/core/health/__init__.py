"""NEXYA Core — Health checks étendus (Session O1 volet B).

Trois exports principaux :
- `detect_version()` : récupère commit_sha + tag git via 3 fallbacks
- `ExtendedHealthService` : agrège db/redis/arq/migration/uptime
- `set_app_start_monotonic()` : posé au lifespan startup pour calculer uptime
"""

from __future__ import annotations

from app.core.health.extended import (
    ExtendedHealthResponse,
    ExtendedHealthService,
    get_app_start_monotonic,
    set_app_start_monotonic,
)
from app.core.health.version import VersionInfo, detect_version

__all__ = [
    "ExtendedHealthResponse",
    "ExtendedHealthService",
    "VersionInfo",
    "detect_version",
    "get_app_start_monotonic",
    "set_app_start_monotonic",
]
