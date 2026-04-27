"""Router admin Helpdesk — `GET /admin/helpdesk/metrics`.

ACL via `require_admin` (J1 — email-based whitelist depuis settings).
Réponse JSON ingestable par le futur dashboard admin V2 (et/ou Grafana
K2 V2 via le scraper Prometheus si on expose les metrics au format
Prometheus).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import require_admin
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.helpdesk.schemas import HelpdeskMetricsResponse
from app.features.helpdesk.service import HelpdeskMetricsService
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/admin/helpdesk", tags=["admin", "helpdesk"])


@router.get("/metrics", response_model=NexyaResponse[HelpdeskMetricsResponse])
async def get_helpdesk_metrics(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[HelpdeskMetricsResponse]:
    """KPI agrégés pour le dashboard admin support.

    Retourne :
    - Counts par status (open / in_progress / resolved / cancelled)
    - `median_resolved_age_hours` (signal qualité support)
    - `oldest_open_age_hours` (signal retard backlog)
    - Breakdown par catégorie
    """
    metrics = await HelpdeskMetricsService.compute(db)
    return NexyaResponse(success=True, data=metrics)
