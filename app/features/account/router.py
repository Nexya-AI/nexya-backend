"""Router Account — `GET /user/quotas` (Session C4.11).

Endpoint read-only qui agrège les quotas user pour le dashboard
Settings > Mon compte. Pas de rate limit V1 (lecture O(1) Postgres+Redis,
appelé ~5 fois/jour/user max au mount Settings + pull-to-refresh).

Pas d'AsyncNotifier ni de mutation V1. L'écran Account côté Flutter
appelle au mount + sur pull-to-refresh — pattern aligné `/library`
listing qui n'est pas non plus throttlé.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.account.schemas import UserQuotasResponse
from app.features.account.service import QuotasService
from app.features.auth.models import User
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/user", tags=["account"])


@router.get(
    "/quotas",
    response_model=NexyaResponse[UserQuotasResponse],
)
async def get_user_quotas(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[UserQuotasResponse]:
    """C4.11 — Snapshot quotas user pour Settings > Mon compte dashboard.

    Retourne :
      - `docs_generated_this_month` + `docs_max_month` (compteur PDF+DOCX
        ce mois UTC vs cap selon plan)
      - `voice_minutes_today` + `voice_minutes_max_day` (Pro only,
        `null` pour Free — UX cache la carte côté Flutter)
      - `library_storage_bytes` + `library_storage_max_bytes` (SOMME
        size_bytes actifs vs cap Free 100MB / Pro 10GB)
      - `reset_at` (prochain 1er du mois UTC minuit)
      - `plan` ('free' ou 'pro')

    Pas de rate limit V1 (lecture O(1) Postgres + Redis, ~5 calls/day/user).
    Fail-safe absolu côté service : Redis down → voice_minutes_today=0
    silencieux pour Pro (l'écran reste consultable).
    """
    snapshot = await QuotasService.compute(current_user, db)
    return NexyaResponse(
        success=True,
        data=UserQuotasResponse(
            docs_generated_this_month=snapshot.docs_generated_this_month,
            docs_max_month=snapshot.docs_max_month,
            voice_minutes_today=snapshot.voice_minutes_today,
            voice_minutes_max_day=snapshot.voice_minutes_max_day,
            library_storage_bytes=snapshot.library_storage_bytes,
            library_storage_max_bytes=snapshot.library_storage_max_bytes,
            reset_at=snapshot.reset_at,
            plan=snapshot.plan,
            chat_messages_used=snapshot.chat_messages_used,
            chat_messages_max=snapshot.chat_messages_max,
            chat_reset_at=snapshot.chat_reset_at,
            images_used_today=snapshot.images_used_today,
            images_max_day=snapshot.images_max_day,
            vision_used_today=snapshot.vision_used_today,
            vision_max_day=snapshot.vision_max_day,
            daily_reset_at=snapshot.daily_reset_at,
        ),
    )
