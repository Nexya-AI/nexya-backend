"""Router POST /code-projects/build-zip (C4.6).

Construit un .zip projet code multi-fichiers en mémoire + upload MinIO
+ retourne presigned URL TTL 24h. Consommé côté Flutter par
`NxCodeProjectCard` au tap « Télécharger .zip » → Dio download +
share_plus partage natif.

Auth requise + rate limit 10/jour/user (anti-abus, modulable via
setting `code_projects_rate_limit_per_day`).

Pas de persistance DB V1. V2 si signal user : table
`code_project_zips` + quota Library type='code' file_type='zip'.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import RateLimitAbuseException
from app.core.security.rate_limiter import check_user_rate_limit
from app.core.storage.object_store import (
    ObjectStoreUnavailableException,
    get_object_store,
)
from app.features.auth.models import User
from app.features.code_projects.schemas import BuildZipRequest, BuildZipResponse
from app.features.code_projects.service import CodeProjectService
from app.shared.schemas import NexyaResponse

log = structlog.get_logger()

router = APIRouter(prefix="/code-projects", tags=["code-projects"])


@router.post(
    "/build-zip",
    response_model=NexyaResponse[BuildZipResponse],
    summary="Construit un .zip projet code multi-fichiers (C4.6)",
)
async def build_zip(
    body: BuildZipRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[BuildZipResponse]:
    """Construit un .zip d'un projet code multi-fichiers en mémoire.

    Consommé par `NxCodeProjectCard.downloadZip` côté Flutter au tap
    sur le bouton « Télécharger .zip ». Le payload `CodeProjectDraftData`
    est le même que celui détecté dans `messages.metadata_json.rich_content`
    (kind=`code_project_draft`) côté backend rich_content C4.6.

    Pipeline :
    1. Rate limit user-scoped 10/jour (`code_projects_rate_limit_per_day`).
    2. Pydantic valide le payload (2-50 fichiers path-safe, cap 5 MB total).
    3. Service construit le .zip en mémoire via `zipfile.ZipFile(BytesIO())`
       + README.md auto-généré.
    4. SHA-256 + storage_key sharded MinIO + upload bytes.
    5. Presigned URL TTL 24h (HMAC local, pas de round-trip).

    Erreurs typées :
    - 401 `AUTH_TOKEN_EXPIRED/INVALID` : JWT requis.
    - 422 `VALIDATION_ERROR` : Pydantic refuse (filename path-unsafe,
      cap fichiers/taille dépassé, etc.).
    - 422 si .zip généré dépasse `code_projects_max_zip_size_mb` (50 MB
      défaut, rare car cap 5 MB texte brut côté payload).
    - 429 `RATE_LIMIT_ABUSE` : quota 10/jour dépassé, data.retry_after en s.
    - 503 `STORAGE_UNAVAILABLE` : MinIO down (rare, fail-safe absolu).
    """
    # 1. Rate limit user-scoped (10/jour défaut).
    await check_user_rate_limit(
        current_user.id,
        action="code_projects_build_zip",
        max_requests=settings.code_projects_rate_limit_per_day,
        window_seconds=24 * 3600,
        on_exceeded=RateLimitAbuseException,
    )

    # 2. Service build zip + upload MinIO + presigned URL.
    object_store = get_object_store()
    try:
        result = await CodeProjectService.build_zip(
            payload=body.payload,
            user_id=current_user.id,
            object_store=object_store,
        )
    except ValueError as exc:
        # Cap dur taille .zip dépassé (rare avec caps Pydantic en amont).
        log.warning(
            "code_projects.build_zip.size_exceeded",
            user_id=str(current_user.id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ObjectStoreUnavailableException:
        # MinIO down — propage 503 (handler global ObjectStoreUnavailableException).
        log.error(
            "code_projects.build_zip.storage_unavailable",
            user_id=str(current_user.id),
        )
        raise

    return NexyaResponse(success=True, data=result)
