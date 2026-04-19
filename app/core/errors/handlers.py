"""
Handlers d'exceptions globaux FastAPI.

Intercepte toutes les exceptions et les transforme en NexyaResponse.
Règle de sécurité : le client ne voit JAMAIS les détails d'une erreur interne.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors.exceptions import NexYaException
from app.shared.schemas import NexyaResponse

log = structlog.get_logger()

# Champs dont la valeur ne doit jamais apparaître dans les logs, même en cas
# d'erreur de validation. Le match est case-insensitive et en sous-chaîne
# (ex: "new_password" matche "password"). À compléter au fil des features.
SENSITIVE_KEYS: tuple[str, ...] = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "webhook_secret",
    "device_token",
)

_REDACTED = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(needle in k for needle in SENSITIVE_KEYS)


def _scrub(value: Any) -> Any:
    """Masque récursivement les valeurs des champs sensibles d'une structure JSON-like."""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_sensitive_key(str(k)) else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _safe_body_preview(body: Any) -> str:
    """Renvoie un aperçu du body de requête en masquant tous les champs sensibles.

    exc.body peut être des bytes, une str JSON, un dict déjà parsé, ou autre.
    On couvre les trois formats les plus fréquents et on tronque à 500 caractères.
    """
    if body is None:
        return ""
    if isinstance(body, bytes):
        try:
            body = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "[binary or non-JSON body]"
    elif isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            return "[non-JSON body]"
    scrubbed = _scrub(body)
    return json.dumps(scrubbed, ensure_ascii=False)[:500]


def _safe_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Scrub le champ `input` de chaque erreur Pydantic pour un chemin sensible."""
    safe: list[dict[str, Any]] = []
    for err in errors:
        loc = err.get("loc", ())
        leaf = loc[-1] if loc else ""
        if isinstance(leaf, str) and _is_sensitive_key(leaf):
            safe.append({**err, "input": _REDACTED})
        else:
            safe.append({**err, "input": _scrub(err.get("input"))})
    return safe


def register_exception_handlers(app: FastAPI) -> None:
    """Enregistre tous les handlers d'exceptions sur l'app FastAPI.

    Appelé une seule fois dans main.py au démarrage.
    """

    @app.exception_handler(NexYaException)
    async def nexya_exception_handler(request: Request, exc: NexYaException) -> JSONResponse:
        """Gère les erreurs métier NEXYA (auth, rate limit, paiement...).

        Le client reçoit un message lisible + un code parsable.
        Les logs serveur contiennent le contexte complet.
        """
        log.warning(
            "nexya.error",
            code=exc.code,
            status=exc.status_code,
            path=request.url.path,
            method=request.method,
        )
        response = NexyaResponse(
            success=False,
            error=exc.message,
            code=exc.code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Gère les erreurs de validation Pydantic (requête malformée).

        Log le body brut + les erreurs détaillées côté serveur.
        Le client reçoit un message générique — pas les détails de validation
        (qui pourraient exposer la structure interne de l'API).
        """
        log.warning(
            "validation.error",
            path=request.url.path,
            method=request.method,
            body=_safe_body_preview(exc.body),
            errors=_safe_errors(exc.errors()),
        )
        response = NexyaResponse(
            success=False,
            error="Données de la requête invalides.",
            code="VALIDATION_ERROR",
        )
        return JSONResponse(
            status_code=422,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Filet de sécurité — intercepte toutes les exceptions non gérées.

        SÉCURITÉ CRITIQUE : le client ne voit JAMAIS la stack trace ni le message
        d'erreur interne. Seul un message générique est retourné.
        Les logs serveur ont le détail complet pour le debug.
        """
        log.error(
            "internal.error",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error=str(exc),
            exc_info=True,
        )
        response = NexyaResponse(
            success=False,
            error="Erreur interne.",
            code="INTERNAL_ERROR",
        )
        return JSONResponse(
            status_code=500,
            content=response.model_dump(mode="json"),
        )
