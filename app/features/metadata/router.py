"""Router Metadata — `POST /metadata/url-preview` (Session C4.2).

Fetch Open Graph tags d'une URL côté serveur avec :
  * Auth requise (`Depends(get_current_user)`) pour rate limit user-scope.
  * Rate limit 60/h/user (`check_user_rate_limit`).
  * Cache Redis 7j sur sha256(url) (cross-user, OG tags publics).
  * Anti-SSRF strict (rejette IPs privées/loopback/link-local).
  * Fail-safe : URL inaccessible → 503 `URL_PREVIEW_UNAVAILABLE`.
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
from app.features.metadata.schemas import (
    UrlPreviewRequest,
    UrlPreviewResponse,
)
from app.features.metadata.url_preview_service import UrlPreviewService
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/metadata", tags=["metadata"])


@router.post(
    "/url-preview",
    response_model=NexyaResponse[UrlPreviewResponse],
    status_code=status.HTTP_200_OK,
)
async def url_preview(
    body: UrlPreviewRequest,
    current_user: User = Depends(get_current_user),
) -> NexyaResponse[UrlPreviewResponse]:
    """Fetch Open Graph tags d'une URL avec cache Redis 7j.

    Rate limit 60/h/user (anti-spam). Cache cross-user (OG tags publics).
    Sur URL inaccessible (DNS down, anti-SSRF rejet, timeout, parsing
    impossible) → 503 `URL_PREVIEW_UNAVAILABLE`.
    """
    # Rate limit pré-flight
    await check_user_rate_limit(
        current_user.id,
        action="url_preview",
        max_requests=settings.url_preview_rate_limit_per_hour,
        window_seconds=3600,
        on_exceeded=RateLimitAbuseException,
    )

    response = await UrlPreviewService.preview(str(body.url))
    if response is None:
        raise NexYaException(
            code="URL_PREVIEW_UNAVAILABLE",
            message="Aperçu de cette URL temporairement indisponible.",
            status_code=503,
        )
    return NexyaResponse(success=True, data=response)
