"""Router AI Models — `GET /models` (Session N1).

Endpoint unique : inventaire runtime des modèles LLM aggrégés depuis
les providers initialisés. Pas de table DB, pas d'écriture, pure
lecture.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.config import settings
from app.core.auth.guards import get_current_user
from app.features.ai_models.schemas import ModelsListResponse
from app.features.ai_models.service import AiModelsService
from app.features.auth.models import User
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/models", tags=["ai_models"])


@router.get(
    "",
    response_model=NexyaResponse[ModelsListResponse],
)
async def list_models(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> NexyaResponse[ModelsListResponse]:
    """Inventaire complet des modèles IA disponibles.

    `Cache-Control: private, max-age=N` — l'état des providers peut
    changer (clé toggle, rotation Mock ↔ réel), donc cache **par-user**
    seulement, pas CDN-public.
    """
    ttl = settings.models_endpoint_cache_ttl_seconds
    response.headers["Cache-Control"] = f"private, max-age={ttl}"
    payload = AiModelsService.list_models()
    return NexyaResponse(success=True, data=payload)
