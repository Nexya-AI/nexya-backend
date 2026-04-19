"""
Trace ID middleware.

Attache un `trace_id` unique à chaque requête HTTP :
- réutilise l'en-tête `X-Request-ID` si le client en fournit un (load balancer,
  CDN Cloudflare, sonde de prod…) — sinon génère un UUID4.
- le binde dans les contextvars structlog pour que tous les logs émis pendant
  la durée de la requête soient corrélés automatiquement.
- l'ajoute à la réponse HTTP pour que le client puisse le remonter dans un
  ticket de support.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

log = structlog.get_logger()

TRACE_HEADER = "X-Request-ID"


def get_trace_id() -> str | None:
    """Accesseur — renvoie le trace_id courant si la requête est en cours."""
    return structlog.contextvars.get_contextvars().get("trace_id")


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Middleware Starlette — un trace_id par requête, corrélé dans tous les logs."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            method=request.method,
            path=request.url.path,
        )

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            log.info(
                "http.access",
                status=status_code,
                duration_ms=duration_ms,
                client=request.client.host if request.client else None,
            )
            structlog.contextvars.clear_contextvars()
