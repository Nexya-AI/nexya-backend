"""NEXYA Core — OpenAPI customization (Session O1).

Enrichit le schéma OpenAPI auto-généré par FastAPI pour le rendre
production-grade (DD-ready) :
- Title/description/contact/license en français
- Tags hiérarchiques avec descriptions par feature
- securitySchemes BearerAuth + PrometheusToken
- servers dev/staging/prod selon settings.env
- examples Pydantic v2 réutilisables

Hook côté `app/main.py` après `include_router` :

    from app.core.openapi import customize_openapi
    app.openapi = lambda: customize_openapi(app)
"""

from __future__ import annotations

from app.core.openapi.customizer import customize_openapi

__all__ = ["customize_openapi"]
