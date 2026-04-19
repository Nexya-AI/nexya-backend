"""
Router racine de l'API v1.

Agrège tous les sous-routers de `app/api/v1/endpoints/` derrière le préfixe
`/v1`, monté ensuite par `app/main.py`.

    # main.py
    from app.api.v1.router import api_v1_router
    app.include_router(api_v1_router, prefix="/v1")

Sera peuplé en PR 4 (versioning `/v1/`). Pour l'instant : router vide —
aucun endpoint exposé, aucun side-effect à l'import.
"""

from fastapi import APIRouter

api_v1_router = APIRouter()
