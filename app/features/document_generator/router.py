"""Router Document Generator — `POST /generate/document` (C4.7a + C4.12).

Endpoints :
    POST /generate/document            — génère (sync 201 OU async 202, C4.12)
    GET  /generate/document/jobs/{id}  — polling d'un job async (C4.12)

Pipeline POST :
    1. Auth via JWT (Depends get_current_user)
    2. Rate limit user-scoped (60/h Free, 100/h Pro)
    3. Body Pydantic validation (template ∈ Literal anti-injection)
    4. `DocumentGeneratorService.generate_or_enqueue` décide sync vs async
       selon la taille du markdown source (C4.12) :
         - sync (≤ seuil)  → rendu immédiat, 201 + DocumentGenerateResponse
         - async (> seuil) → job enqueué arq, 202 + DocumentGenerateAcceptedResponse,
                             push FCM « 📄 doc prêt » au résultat
    5. Mapping erreurs typées → HTTP codes propres
"""

from __future__ import annotations

import io
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
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
from .job_service import DocumentJobService
from .schemas import (
    DocumentGenerateAcceptedResponse,
    DocumentGenerateRequest,
    DocumentGenerateResponse,
    DocumentJobResponse,
)
from .service import (
    DocumentAsyncResult,
    DocumentGeneratorService,
)

router = APIRouter(prefix="/generate", tags=["documents"])


@router.post(
    "/document",
    response_model=NexyaResponse[DocumentGenerateResponse],
    status_code=status.HTTP_201_CREATED,
    responses={
        202: {
            "model": NexyaResponse[DocumentGenerateAcceptedResponse],
            "description": (
                "Document lourd — rendu déporté sur le worker arq. Le client "
                "est prévenu par push FCM « 📄 doc prêt » + deep link vers la "
                "conversation. Peut poller GET /generate/document/jobs/{id}."
            ),
        },
    },
)
async def generate_document(
    body: DocumentGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Génère un document à partir d'un message conversation + template.

    Renvoie **201** + `DocumentGenerateResponse` (rendu synchrone, petit doc)
    OU **202** + `DocumentGenerateAcceptedResponse` (doc lourd > seuil → job
    async, push FCM au résultat — C4.12).

    Codes erreur :
        404 RESOURCE_NOT_FOUND : message inexistant ou pas owned
        403 PLAN_REQUIRED      : Free tente remove_watermark=true
        413 DOCUMENT_SOURCE_TOO_LONG : content > cap chars
        422 TEMPLATE_NOT_FOUND : template slug invalide
        429 RATE_LIMIT_ABUSE   : quota par heure atteint
        503 DOCUMENT_RENDER_FAILED / DOCUMENT_STORAGE_UNAVAILABLE

    Rate limits : Free 60/h, Pro 100/h.
    """
    # Rate limit user-scoped — Pro a plus de budget.
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

    # Délégation à l'orchestrateur (décide sync vs async — C4.12).
    try:
        outcome = await DocumentGeneratorService.generate_or_enqueue(current_user, body, db)
    except TemplateNotFoundError as exc:
        raise NexYaException(code=exc.code, message=str(exc), status_code=422) from exc
    except DocumentSourceTooLongError as exc:
        raise NexYaException(code=exc.code, message=str(exc), status_code=413) from exc
    except DocumentRenderFailedError as exc:
        raise NexYaException(code=exc.code, message=str(exc), status_code=503) from exc
    except DocumentStorageUnavailableError as exc:
        raise NexYaException(code=exc.code, message=str(exc), status_code=503) from exc

    # ── Async (202) : job enqueué, push FCM au résultat ─────────────
    if isinstance(outcome, DocumentAsyncResult):
        job = outcome.job
        accepted = DocumentGenerateAcceptedResponse(
            job_id=job.id,
            conversation_id=job.conversation_id,
            message_id=job.message_id,
            format=job.format,  # type: ignore[arg-type]
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=jsonable_encoder(NexyaResponse(success=True, data=accepted)),
        )

    # ── Sync (201) : rendu immédiat (chemin C4.7 inchangé) ──────────
    return NexyaResponse(success=True, data=outcome.response)


@router.get(
    "/document/jobs/{job_id}",
    response_model=NexyaResponse[DocumentJobResponse],
)
async def get_document_job(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[DocumentJobResponse]:
    """Polling d'un job de génération asynchrone (C4.12).

    Filet de secours si le push FCM est manqué (réseau 2G/3G, app killed).
    Quand `status='done'`, `download_url` est un presigned MinIO FRAIS
    (TTL 30 min, régénéré à chaque appel). 404 IDOR-safe.

    Lecture O(1) sur PK indexée — non rate-limité (polling client borné).
    """
    job = await DocumentJobService.get_owned_job(job_id, current_user, db)
    response = await DocumentJobService.to_response(job, current_user, db)
    return NexyaResponse(success=True, data=response)


@router.get(
    "/document/download/{library_id}",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {
                "application/pdf": {},
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
            },
            "description": "Binaire du document généré (PDF ou DOCX), streamé depuis MinIO interne.",
        },
        404: {
            "description": (
                "RESOURCE_NOT_FOUND — document inexistant / pas owned / "
                "soft-deleted / blob purgé (IDOR-safe)."
            )
        },
        429: {"description": "RATE_LIMIT_ABUSE — trop de téléchargements/heure."},
        503: {"description": "DOCUMENT_STORAGE_UNAVAILABLE — MinIO injoignable."},
    },
)
async def download_document(
    library_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream le binaire d'un document généré (fix P0 2026-06-10).

    REMPLACE le presigned MinIO qui était **injoignable depuis le téléphone** :
    en prod MinIO n'a aucun port public (réseau Docker interne) et Caddy ne route
    que `api.nexyalabs.com`. L'API, elle, est joignable (TLS + Caddy) ET dans le
    réseau Docker → elle proxy le binaire depuis MinIO interne via
    `object_store.download_bytes`. Le client (dio `apiClientProvider`,
    baseUrl=api.nexyalabs.com + JWT) résout le chemin relatif renvoyé dans
    `download_url` et attache le Bearer automatiquement.

    Auth JWT obligatoire + owner-check IDOR-safe (404 si pas owned / soft-deleted).
    Restreint au type `document` (un image/audio owned passe par les endpoints
    Library, pas par celui-ci).

    Codes d'erreur :
        404 RESOURCE_NOT_FOUND : document KO / pas owned / blob absent (IDOR-safe).
        429 RATE_LIMIT_ABUSE   : > quota téléchargements/heure.
        503 DOCUMENT_STORAGE_UNAVAILABLE : MinIO down.
    """
    await check_user_rate_limit(
        current_user.id,
        action="document_download",
        max_requests=settings.documents_generator_download_rate_limit_per_hour,
        window_seconds=3600,
        on_exceeded=RateLimitAbuseException,
    )

    try:
        data, mime_type, filename = await DocumentGeneratorService.fetch_for_download(
            library_id, current_user, db
        )
    except DocumentStorageUnavailableError as exc:
        raise NexYaException(code=exc.code, message=str(exc), status_code=503) from exc

    # StreamingResponse proxy bytes (le doc tient en RAM, cap pages borne la taille).
    # `ResourceNotFoundException` (404 IDOR) remonte telle quelle au handler global.
    return StreamingResponse(
        content=io.BytesIO(data),
        media_type=mime_type,
        headers={
            "Content-Length": str(len(data)),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )
