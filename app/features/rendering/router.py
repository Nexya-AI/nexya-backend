"""Router Rendering — `POST /render/mermaid` (Session C4.3).

Délègue à `MermaidRenderer.render` qui :
  * Cache Redis 7j sur sha256(source).
  * Délègue à `https://kroki.io/mermaid/svg` via httpx (timeout 10s).
  * Fail-safe : Kroki down → raise `MermaidRenderFailedError` → 503
    `MERMAID_RENDER_FAILED`.

Auth required + rate limit 30/h/user (rendu plus coûteux que url preview).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.errors.exceptions import (
    NexYaException,
    RateLimitAbuseException,
)
from app.core.security.rate_limiter import check_user_rate_limit
from app.features.auth.models import User
from app.features.rendering.mermaid_renderer import (
    MermaidRenderer,
    MermaidRenderFailedError,
)
from app.features.rendering.schemas import (
    MermaidRenderRequest,
    MermaidRenderResponse,
)
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/render", tags=["rendering"])


@router.post(
    "/mermaid",
    response_model=NexyaResponse[MermaidRenderResponse],
    status_code=status.HTTP_200_OK,
)
async def render_mermaid(
    body: MermaidRenderRequest,
    current_user: User = Depends(get_current_user),
) -> NexyaResponse[MermaidRenderResponse]:
    """Rend un diagramme Mermaid en SVG via Kroki.io avec cache Redis 7j.

    Rate limit 30/h/user (rendu plus coûteux que url preview).
    Sur échec Kroki (down/timeout/5xx) → 503 `MERMAID_RENDER_FAILED`.
    """
    await check_user_rate_limit(
        current_user.id,
        action="mermaid_render",
        max_requests=settings.mermaid_render_rate_limit_per_hour,
        window_seconds=3600,
        on_exceeded=RateLimitAbuseException,
    )

    try:
        result = await MermaidRenderer.render(body.source)
    except MermaidRenderFailedError as exc:
        raise NexYaException(
            code="MERMAID_RENDER_FAILED",
            message="Rendu du diagramme temporairement indisponible.",
            status_code=503,
        ) from exc
    return NexyaResponse(success=True, data=result)
