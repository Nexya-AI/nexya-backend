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
            k: (_REDACTED if _is_sensitive_key(str(k)) else _scrub(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


# K1 — Alias public utilisé par le pont Sentry (`core/observability/sentry.py`)
# pour scruber `event["request"]["data"]`, `event["extra"]`, `event["contexts"]`,
# `event["breadcrumbs"]` AVANT envoi à Sentry. Volontairement un alias plutôt
# qu'un déplacement de fichier : les ~38 tests A3 hardening importent `_scrub`
# directement, on évite la régression.
scrub_secrets = _scrub


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

        N4 (Phase 18) : hook escalation Crisp fire-and-forget pour les
        incidents critiques (paiement, LLM down) côté users Pro. Le hook
        est fail-safe absolu — il ne fait jamais cascade sur la 500 user.
        """
        log.warning(
            "nexya.error",
            code=exc.code,
            status=exc.status_code,
            path=request.url.path,
            method=request.method,
        )

        # N4 — escalation Crisp fire-and-forget (Pro user + incident critique)
        _maybe_escalate_to_crisp(request=request, exc=exc)

        response = NexyaResponse(
            success=False,
            error=exc.message,
            code=exc.code,
            data=exc.data,
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


# ═══════════════════════════════════════════════════════════════════
# N4 — Hook escalation Crisp (fire-and-forget, fail-safe absolu)
# ═══════════════════════════════════════════════════════════════════


def _maybe_escalate_to_crisp(*, request: Request, exc: NexYaException) -> None:
    """Schedule une escalation Crisp si l'incident le justifie.

    Pipeline minimal :
    1. Map `exc.code` → category Helpdesk (payment / llm_unavailable /
       data_loss / rgpd / security). Inconnu → return.
    2. Lit `request.state.current_user` (posé par le guard
       `get_current_user` quand l'endpoint est authentifié). None → skip.
    3. Vérifie `CrispEscalationService.should_escalate` (Pro + severity
       élevée + kill-switch).
    4. Lance la coroutine via `asyncio.create_task` — fire-and-forget.

    Fail-safe : toute exception levée ici est swallowed. Le caller
    (handler global) ne doit JAMAIS être impacté.
    """
    try:
        category = _CRISP_CATEGORY_BY_CODE.get(exc.code)
        if category is None:
            return

        user = getattr(getattr(request, "state", None), "current_user", None)

        # Import paresseux pour éviter circular imports + ne pas charger
        # le module helpdesk si l'escalation n'est jamais déclenchée.
        from app.features.helpdesk.service import CrispEscalationService  # noqa: PLC0415
        from app.features.helpdesk.schemas import EscalationCreate  # noqa: PLC0415

        severity = _CRISP_SEVERITY_BY_CODE.get(exc.code, "high")
        if not CrispEscalationService.should_escalate(
            user=user, category=category, severity=severity
        ):
            return

        body = EscalationCreate(
            user_id=getattr(user, "id", None),
            category=category,
            severity=severity,
            payload={
                "exc_code": exc.code,
                "exc_message": exc.message[:500],
                "request_path": str(request.url.path),
                "request_method": request.method,
                **(exc.data or {}),
            },
        )

        # Fire-and-forget — on ouvre une session DB indépendante pour
        # ne pas tenir la session de la requête (qui peut être déjà
        # rollback-ée par l'exception courante).
        import asyncio  # noqa: PLC0415

        asyncio.create_task(_run_escalation(body=body, user=user))
    except Exception as fail:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "crisp.escalation.hook_failed",
            error=str(fail),
            exc_type=type(fail).__name__,
        )


_CRISP_CATEGORY_BY_CODE: dict[str, str] = {
    "PAYMENT_FAILED": "payment",
    "PAYMENT_WEBHOOK_INVALID": "payment",
    "LLM_UNAVAILABLE": "llm_unavailable",
}


_CRISP_SEVERITY_BY_CODE: dict[str, str] = {
    "PAYMENT_FAILED": "high",
    "PAYMENT_WEBHOOK_INVALID": "critical",
    "LLM_UNAVAILABLE": "high",
}


async def _run_escalation(*, body, user) -> None:  # type: ignore[no-untyped-def]
    """Coroutine encapsulée qui ouvre une session DB fraîche puis
    délègue à `CrispEscalationService.escalate`. Fail-safe absolu."""
    try:
        from app.core.database.postgres import AsyncSessionLocal  # noqa: PLC0415
        from app.features.helpdesk.service import CrispEscalationService  # noqa: PLC0415

        async with AsyncSessionLocal() as db:
            await CrispEscalationService.escalate(body=body, user=user, db=db)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "crisp.escalation.task_failed",
            error=str(exc),
            exc_type=type(exc).__name__,
        )
