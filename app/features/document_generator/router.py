"""Router Document Generator — `POST /generate/document` (Session C4.7a).

Endpoint principal pour la génération PDF premium via WeasyPrint.

Pipeline :
    1. Auth via JWT (Depends get_current_user)
    2. Rate limit user-scoped (60/h Free, 100/h Pro)
    3. Body Pydantic validation (template ∈ Literal anti-injection)
    4. Délégation `DocumentGeneratorService.generate` qui orchestre
       tout (récup message + render Jinja2 + WeasyPrint + Library)
    5. Mapping erreurs typées → HTTP codes propres
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    NexYaException,
    RateLimitAbuseException,
)
from app.core.security.rate_limiter import check_user_rate_limit
from app.features.auth.models import User
from app.shared.schemas import NexyaResponse

from .exceptions import (
    DocumentRenderFailedError,
    DocumentSourceTooLongError,
    DocumentStorageUnavailableError,
    TemplateNotFoundError,
)
from .schemas import DocumentGenerateRequest, DocumentGenerateResponse
from .service import DocumentGeneratorService

router = APIRouter(prefix="/generate", tags=["documents"])


@router.post(
    "/document",
    response_model=NexyaResponse[DocumentGenerateResponse],
    status_code=status.HTTP_201_CREATED,
)
async def generate_document(
    body: DocumentGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[DocumentGenerateResponse]:
    """Génère un PDF à partir d'un message conversation + template.

    Body :
        conversation_id : UUID conversation source
        message_id : UUID message assistant dont content = source markdown
        format : 'pdf' (V1 seul)
        template : 'school' | 'minimal'
        options : { subject?, level?, date_iso?, page_numbers? }
        title? : titre principal (optional, dérivé sinon)

    Returns :
        DocumentGenerateResponse avec library_id + download_url presigned
        TTL 30 min + metadata pages/size/truncated/expires_at.

    Codes erreur :
        404 RESOURCE_NOT_FOUND : message inexistant ou pas owned
        413 DOCUMENT_SOURCE_TOO_LONG : content > 200k chars
        422 TEMPLATE_NOT_FOUND : template slug invalide (rare, Pydantic
            Literal devrait empêcher en amont)
        429 RATE_LIMIT_ABUSE : quota par heure atteint
        503 DOCUMENT_RENDER_FAILED : WeasyPrint timeout/crash
        503 DOCUMENT_STORAGE_UNAVAILABLE : MinIO down

    Rate limits :
        Free : 60/h (cf. settings.documents_generator_rate_limit_free_per_hour)
        Pro  : 100/h (cf. settings.documents_generator_rate_limit_pro_per_hour)
    """
    # Rate limit user-scoped — Pro a plus de budget
    max_requests = (
        settings.documents_generator_rate_limit_pro_per_hour
        if current_user.is_pro
        else settings.documents_generator_rate_limit_free_per_hour
    )
    await check_user_rate_limit(
        current_user.id,
        action="document_generate",
        max_requests=max_requests,
        window_seconds=3600,
        on_exceeded=RateLimitAbuseException,
    )

    # Délégation au service (orchestrateur)
    try:
        result = await DocumentGeneratorService.generate(current_user, body, db)
    except TemplateNotFoundError as exc:
        raise NexYaException(
            code=exc.code,
            message=str(exc),
            status_code=422,
        ) from exc
    except DocumentSourceTooLongError as exc:
        raise NexYaException(
            code=exc.code,
            message=str(exc),
            status_code=413,
        ) from exc
    except DocumentRenderFailedError as exc:
        raise NexYaException(
            code=exc.code,
            message=str(exc),
            status_code=503,
        ) from exc
    except DocumentStorageUnavailableError as exc:
        raise NexYaException(
            code=exc.code,
            message=str(exc),
            status_code=503,
        ) from exc

    return NexyaResponse(success=True, data=result)
