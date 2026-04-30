"""
Router Vision — endpoint `POST /vision/analyze` (E2).

Pas de `require_pro` gate : Free autorisé avec tier=`flash` imposé.
Pro accède aux tiers flash + pro. Le contrôle tier se fait dans le
service (`VisionService.analyze` étape 1) → 403 `PLAN_REQUIRED` si Free
tente un tier='pro'.

Discipline :
- Aucune logique métier ici — délégation stricte à `VisionService`.
- Query params / body Pydantic gèrent les validations syntaxiques.
- Rate limit côté service (distinct du budget jour).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.vision.schemas import (
    VisionAnalysisResponse,
    VisionAnalyzeRequest,
)
from app.features.vision.service import VisionService
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/vision", tags=["vision"])


@router.post(
    "/analyze",
    status_code=status.HTTP_201_CREATED,
    response_model=NexyaResponse[VisionAnalysisResponse],
)
async def analyze(
    body: VisionAnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[VisionAnalysisResponse]:
    """Analyse une image (et jusqu'à 3 additionnelles) via LLM multimodal.

    3 modes d'entrée mutex :
    - `upload_id` : UUID d'un UploadedFile (E3) déjà uploadé.
    - `library_id` : UUID d'un LibraryItem image (C3).
    - `image_base64` : data URL ou base64 brut direct.

    `model_tier` :
    - `flash` : Gemini 2.0 Flash (Free + Pro, cheapest).
    - `pro` : Gemini 2.0 Pro ou GPT-4o (**Pro only** → 403 PLAN_REQUIRED pour Free).

    Consomme 1 slot `vision_images_{free,pro}_per_day` + rate limit
    `vision_rate_limit_per_hour` (30/h). Dédup SHA transparente sur
    `(user, image, prompt)`.
    """
    row = await VisionService.analyze(current_user, db, body=body)
    return NexyaResponse(success=True, data=VisionAnalysisResponse.model_validate(row))
