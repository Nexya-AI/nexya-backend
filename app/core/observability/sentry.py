"""
Pont Sentry — capture des exceptions non rattrapées + breadcrumbs.

Pattern env-aware :
- `SENTRY_DSN=""` (défaut) → `sentry_sdk.init` n'est PAS appelé (zéro
  overhead, zéro outbound, zéro dépendance réseau). Le service tourne
  exactement comme avant l'ajout de Sentry.
- `SENTRY_DSN` rempli → init avec 5 integrations standards + scrubber
  secrets ponté depuis `core/errors/handlers.py` (import direct).

Fail-safe absolu : toute exception levée pendant l'init est attrapée
et loguée comme warning. Le service NEXYA continue à tourner — Sentry
KO ne doit JAMAIS faire crasher l'API.

Le scrubber A3 est réutilisé tel quel (alias public `scrub_secrets`)
pour ne pas dupliquer la logique des champs sensibles. Tout event
Sentry passe par `_sentry_scrub_event` avant envoi : `request.data`,
`extra`, `contexts`, `breadcrumbs.data` sont nettoyés récursivement.

Erreurs filtrées (pas de bruit Sentry sur les erreurs métier
normales) :
- `asyncio.CancelledError` — l'user a coupé le SSE, pas un bug.
- `ProviderContentFilteredError` — le filtre a fait son travail.
- `ResourceNotFoundException` — 404 IDOR-safe, métier normal.
- `NexYaException` (générique) — déjà tracé en log warning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import structlog

from app.config import Settings, settings
from app.core.errors.exceptions import (
    NexYaException,
    ResourceNotFoundException,
)
from app.core.errors.handlers import scrub_secrets

log = structlog.get_logger(__name__)


# Liste mutable au niveau module — testée via vérification d'état
# (les tests peuvent observer si init a été tenté ou non).
_INIT_ATTEMPTED = False
_INIT_OK = False


def _sentry_scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Hook `before_send` — scrub les secrets avant que Sentry les voie.

    On nettoie récursivement les zones où l'user-payload peut atterrir :
    - `event["request"]["data"]` → body POST/PATCH brut
    - `event["request"]["headers"]` → Authorization, X-Api-Key, etc.
    - `event["request"]["query_string"]` → ?token=… dans l'URL
    - `event["extra"]` → contexte custom posé via `set_extra`
    - `event["contexts"]` → contextes typés (custom, runtime…)
    - `event["breadcrumbs"]["values"][i]["data"]` → trail des actions
    """
    try:
        request = event.get("request")
        if isinstance(request, dict):
            for key in ("data", "headers", "query_string", "cookies"):
                if key in request:
                    request[key] = scrub_secrets(request[key])

        if "extra" in event:
            event["extra"] = scrub_secrets(event["extra"])

        if "contexts" in event:
            event["contexts"] = scrub_secrets(event["contexts"])

        breadcrumbs = event.get("breadcrumbs")
        if isinstance(breadcrumbs, dict):
            values = breadcrumbs.get("values")
            if isinstance(values, list):
                for crumb in values:
                    if isinstance(crumb, dict) and "data" in crumb:
                        crumb["data"] = scrub_secrets(crumb["data"])
    except Exception as exc:  # noqa: BLE001 — scrub ne doit jamais crasher
        log.warning("sentry.scrub.failed", error=str(exc))
        # On retourne None pour drop l'event si le scrubber a planté —
        # mieux vaut perdre un event que fuiter un secret par accident.
        return None

    return event


def _should_capture(exc_type: type[BaseException] | None) -> bool:
    """Filtre — décide si l'exception mérite un event Sentry."""
    if exc_type is None:
        return True
    if issubclass(exc_type, asyncio.CancelledError):
        return False
    # Toutes les NexYaException métier (404 IDOR, validation, rate
    # limit, plan required, etc.) sont déjà loguées en warning par
    # le handler global — pas besoin de spam Sentry.
    if issubclass(exc_type, NexYaException):
        return False
    if issubclass(exc_type, ResourceNotFoundException):
        return False
    return True


def setup_sentry(cfg: Settings | None = None) -> bool:
    """Initialise Sentry si DSN renseigné. Idempotent + fail-safe.

    Retourne True si Sentry est actif après l'appel, False sinon
    (DSN vide, exception d'init, ou SDK non installé).
    """
    global _INIT_ATTEMPTED, _INIT_OK

    cfg = cfg or settings
    if _INIT_ATTEMPTED:
        return _INIT_OK
    _INIT_ATTEMPTED = True

    dsn = (cfg.sentry_dsn or "").strip()
    if not dsn:
        log.info("sentry.disabled", reason="dsn_empty")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.redis import RedisIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError as exc:
        log.warning("sentry.import_failed", error=str(exc))
        return False

    try:
        integrations = [
            FastApiIntegration(),
            SqlalchemyIntegration(),
            HttpxIntegration(),
            RedisIntegration(),
            AsyncioIntegration(),
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ]

        def _before_send(event, hint):
            exc_info = hint.get("exc_info") if isinstance(hint, dict) else None
            exc_type = exc_info[0] if exc_info else None
            if not _should_capture(exc_type):
                return None
            return _sentry_scrub_event(event, hint or {})

        sentry_sdk.init(
            dsn=dsn,
            environment=cfg.sentry_environment,
            release=cfg.app_version,
            traces_sample_rate=cfg.sentry_traces_sample_rate,
            profiles_sample_rate=cfg.sentry_profiles_sample_rate,
            integrations=integrations,
            before_send=_before_send,
            send_default_pii=False,  # RGPD : pas d'IP, pas d'identifiants
        )
        _INIT_OK = True
        log.info(
            "sentry.initialized",
            environment=cfg.sentry_environment,
            release=cfg.app_version,
            traces_sample_rate=cfg.sentry_traces_sample_rate,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "sentry.init_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


async def shutdown_sentry() -> None:
    """Flush les events en attente avant arrêt. Fail-safe."""
    if not _INIT_OK:
        return
    try:
        import sentry_sdk

        client = sentry_sdk.Hub.current.client
        if client is not None:
            # Flush 2 s max : on ne bloque pas le shutdown indéfiniment.
            client.flush(timeout=2.0)
    except Exception as exc:  # noqa: BLE001
        log.warning("sentry.flush_failed", error=str(exc))


def is_initialized() -> bool:
    """Helper testable — retourne True si Sentry est actif."""
    return _INIT_OK


def _reset_for_tests() -> None:
    """Hook test-only — réinitialise les flags module pour permettre
    plusieurs setup_sentry() successifs dans la même suite pytest."""
    global _INIT_ATTEMPTED, _INIT_OK
    _INIT_ATTEMPTED = False
    _INIT_OK = False
