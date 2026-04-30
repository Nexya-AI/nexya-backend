"""Router RGPD — endpoints publics + 1 admin (Session J1).

5 endpoints publics + 1 admin :

| Endpoint                                              | Auth   | Description                                         |
|-------------------------------------------------------|--------|-----------------------------------------------------|
| GET  /rgpd/user/data-export                           | user   | ZIP archive complète (Articles 15+20)               |
| GET  /rgpd/user/consent                               | user   | Liste des consentements actifs                      |
| POST /rgpd/user/consent                               | user   | Record / revoke consentement (Article 7)            |
| POST /rgpd/user/account/delete-request                | user   | Crée DeletionRequest pending (Article 17)           |
| POST /rgpd/user/account/delete-request/cancel         | user   | Rétractation user avant J+grace_period              |
| GET  /rgpd/admin/ai-act-registry                      | admin  | Export CSV/JSON registre IA (AI Act Article 13)    |
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user, require_admin
from app.core.database.postgres import get_db
from app.core.errors.exceptions import RateLimitAbuseException
from app.core.security.rate_limiter import check_user_rate_limit
from app.features.auth.auth_events import log_auth_event
from app.features.auth.models import User
from app.features.rgpd.ai_act_registry_service import AIActRegistryService
from app.features.rgpd.consent_service import ConsentService
from app.features.rgpd.data_export_service import DataExportService
from app.features.rgpd.deletion_service import DeletionRequestService
from app.features.rgpd.schemas import (
    AIActRegistryFilters,
    ConsentListResponse,
    ConsentRecordRequest,
    ConsentResponse,
    DeletionRequestCreate,
    DeletionRequestResponse,
)
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/rgpd", tags=["rgpd"])


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    ua = request.headers.get("user-agent")
    return ua.strip() if ua else None


# ══════════════════════════════════════════════════════════════
# DATA EXPORT — Articles 15 + 20
# ══════════════════════════════════════════════════════════════


@router.get("/user/data-export")
async def data_export(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Export ZIP complet des données utilisateur.

    Rate limit : 1 export / 24h (anti-abus).
    """
    # Rate limit user-scope
    await check_user_rate_limit(
        current_user.id,
        action="rgpd_data_export",
        max_requests=settings.rgpd_export_rate_limit_per_24h,
        window_seconds=24 * 3600,
        on_exceeded=RateLimitAbuseException,
    )

    service = DataExportService()
    result = await service.build_export(current_user, db)

    # Audit (preuve d'envoi de l'export)
    await log_auth_event(
        db,
        event_type="data_exported",
        user_id=current_user.id,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        metadata={
            "size_bytes": len(result.zip_bytes),
            "truncated": result.truncated,
        },
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"nexya-export-{current_user.id}-{timestamp}.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(result.zip_bytes)),
        "X-Export-Truncated": "true" if result.truncated else "false",
    }

    def _iter_bytes():
        # Générateur 1-shot — le ZIP est déjà en mémoire.
        yield result.zip_bytes

    return StreamingResponse(
        _iter_bytes(),
        media_type="application/zip",
        headers=headers,
    )


# ══════════════════════════════════════════════════════════════
# CONSENT — Article 7
# ══════════════════════════════════════════════════════════════


@router.get("/user/consent", response_model=NexyaResponse[ConsentListResponse])
async def list_consents(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConsentListResponse]:
    rows = await ConsentService.list_for_user(current_user, db)
    items = [ConsentResponse.model_validate(r) for r in rows]
    return NexyaResponse(success=True, data=ConsentListResponse(items=items))


@router.post(
    "/user/consent",
    response_model=NexyaResponse[ConsentResponse],
    status_code=201,
)
async def record_consent(
    body: ConsentRecordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConsentResponse]:
    """Record un consentement (idempotent sur même version)."""
    row = await ConsentService.record(
        current_user,
        body,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        db=db,
    )
    return NexyaResponse(success=True, data=ConsentResponse.model_validate(row))


@router.delete(
    "/user/consent/{consent_type}",
    status_code=204,
)
async def revoke_consent(
    consent_type: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Révoque un consentement (idempotent — 204 même si pas actif)."""
    await ConsentService.revoke(
        current_user,
        consent_type,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        db=db,
    )
    return Response(status_code=204)


# ══════════════════════════════════════════════════════════════
# ACCOUNT DELETION — Article 17 (workflow 2-step)
# ══════════════════════════════════════════════════════════════


@router.post(
    "/user/account/delete-request",
    response_model=NexyaResponse[DeletionRequestResponse],
    status_code=202,
)
async def create_delete_request(
    body: DeletionRequestCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[DeletionRequestResponse]:
    """Crée une demande de suppression (workflow 2-step)."""
    dr = await DeletionRequestService.create_request(
        current_user,
        reason=body.reason,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        db=db,
    )
    return NexyaResponse(success=True, data=DeletionRequestResponse.model_validate(dr))


@router.post(
    "/user/account/delete-request/cancel",
    response_model=NexyaResponse[DeletionRequestResponse],
)
async def cancel_delete_request(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[DeletionRequestResponse]:
    """Annule la demande de suppression active (rétractation user)."""
    dr = await DeletionRequestService.cancel_request(
        current_user,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
        db=db,
    )
    return NexyaResponse(success=True, data=DeletionRequestResponse.model_validate(dr))


# ══════════════════════════════════════════════════════════════
# ADMIN — AI Act Registry
# ══════════════════════════════════════════════════════════════


@router.get("/admin/ai-act-registry")
async def admin_ai_act_registry(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    format: str = Query(default="csv", pattern=r"^(csv|json)$"),
    limit: int = Query(default=10_000, ge=1, le=100_000),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export du registre AI Act — admin only."""
    filters = AIActRegistryFilters(
        date_from=date_from,
        date_to=date_to,
        format=format,
        limit=limit,
    )
    rows = await AIActRegistryService.fetch_rows(filters, db)
    if format == "csv":
        body = AIActRegistryService.export_csv(rows)
        media = "text/csv; charset=utf-8"
        ext = "csv"
    else:
        body = AIActRegistryService.export_json(rows)
        media = "application/json"
        ext = "json"

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"nexya-ai-act-registry-{timestamp}.{ext}"
    return Response(
        content=body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(body)),
        },
    )
