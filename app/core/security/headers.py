"""
Middleware Headers Sécurité — `NexyaSecurityHeadersMiddleware` (O1 volet C).

Pose les headers de sécurité HTTP (CSP / HSTS / X-Frame-Options /
X-Content-Type-Options / Referrer-Policy / Permissions-Policy / COOP /
CORP) selon le preset configuré dans `settings.security_headers_preset`.

4 presets disponibles :

| Preset | Quand | Headers posés |
|--------|-------|---------------|
| `dev` | Dev local (Swagger doit fonctionner) | `X-Content-Type-Options: nosniff` seul |
| `staging` | Staging post-L2 | + HSTS court + X-Frame-Options + Referrer + Permissions + CSP `unsafe-inline` |
| `prod` | Production | + HSTS preload + COOP same-origin + CORP same-origin + CSP strict (sans unsafe-inline) |
| `off` | Kill-switch incident | aucun header |

**Skip CSP sur `/docs`, `/redoc`, `/openapi.json`** quand preset ∈
{dev, staging} — Swagger UI utilise inline JS pour son interface.
En `prod`, ces paths sont déjà désactivés via `docs_url=None` côté
`FastAPI(...)` donc on n'a pas besoin de les skip.

**Production safety guard** dans `Settings._enforce_production_safety` :
`is_production AND security_headers_preset NOT IN ('prod','off')` →
ValueError fail-fast au boot. On accepte `off` comme kill-switch
incident ponctuel.

Pédagogie HSTS preload :
- `max-age=31536000` = 1 an d'engagement HTTPS strict.
- `includeSubDomains` = couvre tous les sous-domaines.
- `preload` = soumission [hstspreload.org](https://hstspreload.org/) →
  navigateurs hardcode la liste. Quasi-impossible de revenir à HTTP
  après. À ne faire que post-L2 staging stable.
"""

from __future__ import annotations

from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# ═══════════════════════════════════════════════════════════════════
# PRESETS
# ═══════════════════════════════════════════════════════════════════
# Un dict vide = preset `off` (rien posé).
# Les valeurs sont des dict {header_name: header_value} appliqués
# tels quels sur la réponse.
# ═══════════════════════════════════════════════════════════════════


_HEADERS_DEV: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
}


_HEADERS_STAGING: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=()"
    ),
    "Content-Security-Policy": (
        "default-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self' https:; "
        "frame-ancestors 'none'"
    ),
}


_HEADERS_PROD: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=()"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # CSP strict en prod — pas d'unsafe-inline. NEXYA backend = JSON API
    # only, aucun HTML user-facing rendu côté serveur, donc pas besoin.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self' https:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "object-src 'none'"
    ),
}


_HEADERS_OFF: Final[dict[str, str]] = {}


_PRESET_TO_HEADERS: Final[dict[str, dict[str, str]]] = {
    "dev": _HEADERS_DEV,
    "staging": _HEADERS_STAGING,
    "prod": _HEADERS_PROD,
    "off": _HEADERS_OFF,
}


# Paths pour lesquels on retire la CSP en preset != prod (Swagger UI inline JS)
_SWAGGER_PATHS: Final[frozenset[str]] = frozenset(
    {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)


# ═══════════════════════════════════════════════════════════════════
# MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════


class NexyaSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Pose les headers de sécurité selon le preset configuré.

    Le preset est figé à la construction (lecture `settings`). Pour
    changer en runtime → redémarrer l'app (settings = source de vérité,
    pas de reload chaud, anti-divergence dev/prod).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        preset: str = "dev",
    ) -> None:
        super().__init__(app)
        if preset not in _PRESET_TO_HEADERS:
            raise ValueError(f"Preset inconnu : {preset!r}. Utilise dev|staging|prod|off.")
        self._preset = preset
        self._headers = _PRESET_TO_HEADERS[preset]

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if not self._headers:
            return response

        # Skip CSP pour Swagger UI en dev/staging (inline JS nécessaire)
        skip_csp = self._preset != "prod" and request.url.path in _SWAGGER_PATHS

        for name, value in self._headers.items():
            if skip_csp and name == "Content-Security-Policy":
                continue
            # Ne pas écraser si l'endpoint a déjà posé son header
            # (ex: `/metrics` qui pose `Cache-Control` custom).
            if name not in response.headers:
                response.headers[name] = value
        return response

    @property
    def preset(self) -> str:
        return self._preset
