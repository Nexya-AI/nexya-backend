"""
Router Files — `POST /files/upload` + `GET /files/{id}` (Sessions E3 + D2.5).

E3 (2026-04-24) a livré le pipeline d'upload complet (MIME → cap → magic →
dédup → scan → upload → INSERT → extract). D2.5 (2026-05-04) ajoute le
endpoint de lecture `GET /files/{id}` que le client Flutter consomme pour
poller la sentinelle d'indexation RAG `chunks_indexed_at` (D4) après upload
d'un PDF/DOCX/TXT/MD : le worker `index_document_chunks` arq écrit la
sentinelle quand l'indexation pgvector est complète, le client repère le
flip et bascule son badge UI « Indexation… » → « Prêt pour RAG ».

Toute la logique métier vit dans `FileUploadService` — le router se contente
du rate limit, de l'enrichissement presigned URL, et de la traduction
ORM → Pydantic.

Rate limiting : 20 uploads/heure/user via `check_user_rate_limit`. Protège
contre un user qui saturerait notre storage + CPU (extraction) en boucle.
Le `GET /files/{id}` n'est PAS rate-limité (lecture O(1) par PK indexée +
le polling client est borné à ~10 hits/upload sur 5 min).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import RateLimitAbuseException
from app.core.security.rate_limiter import check_user_rate_limit
from app.features.auth.models import User
from app.features.files.models import UploadedFile
from app.features.files.schemas import UploadedFileResponse
from app.features.files.service import FileUploadService, build_text_preview
from app.shared.schemas import NexyaResponse

log = structlog.get_logger()

router = APIRouter(prefix="/files", tags=["files"])


async def _upload_to_response(upload: UploadedFile) -> UploadedFileResponse:
    """Combine une row UploadedFile ORM avec sa presigned URL fraîche +
    preview texte tronqué."""
    url = await FileUploadService.presigned_url_for(upload)
    return UploadedFileResponse(
        id=upload.id,
        user_id=upload.user_id,
        storage_key=upload.storage_key,
        content_sha256=upload.content_sha256,
        size_bytes=upload.size_bytes,
        mime_type=upload.mime_type,
        original_filename=upload.original_filename,
        extension=upload.extension,
        virus_scan_status=upload.virus_scan_status,  # type: ignore[arg-type]
        virus_scan_signature=upload.virus_scan_signature,
        virus_scan_scanner=upload.virus_scan_scanner,
        virus_scanned_at=upload.virus_scanned_at,
        extraction_status=upload.extraction_status,  # type: ignore[arg-type]
        extracted_text_preview=build_text_preview(upload.extracted_text),
        extracted_text_length=upload.extracted_text_length,
        page_count=upload.page_count,
        extraction_truncated=upload.extraction_truncated,
        extracted_at=upload.extracted_at,
        url=url,
        chunks_indexed_at=upload.chunks_indexed_at,
        created_at=upload.created_at,
        updated_at=upload.updated_at,
        deleted_at=upload.deleted_at,
    )


@router.post(
    "/upload",
    response_model=NexyaResponse[UploadedFileResponse],
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    file: UploadFile = File(
        ...,
        description="Le fichier à uploader (multipart/form-data).",
    ),
    extract_text: bool = Query(
        default=True,
        description="Extraire le texte du fichier (PDF/DOCX/plain text).",
    ),
    scan_virus: bool = Query(
        default=True,
        description="Scanner le fichier via le virus scanner configuré.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[UploadedFileResponse]:
    """Upload d'un fichier utilisateur générique.

    Codes d'erreur :
    - **415** `FILE_TYPE_NOT_ALLOWED` si le MIME annoncé est hors whitelist.
    - **415** `FILE_CONTENT_MISMATCH` si le MIME détecté par magic-bytes
      ne correspond pas au MIME annoncé (anti-smuggling).
    - **415** `VIRUS_DETECTED` si le scanner flag le contenu comme
      suspicious. Message utilisateur neutre.
    - **413** `FILE_TOO_LARGE` si le binaire excède
      `settings.files_max_upload_bytes` (100 MB par défaut).
    - **429** `RATE_LIMIT_ABUSE` si > 20 uploads/heure/user.
    - **503** `STORAGE_UNAVAILABLE` si MinIO/S3 down.

    Dédup naturelle : si l'user a déjà uploadé le même contenu (SHA-256
    identique), on retourne l'entrée existante sans erreur ni double-upload.

    Le client peut ensuite attacher cet upload à un projet via
    `POST /projects/{project_id}/files` en passant `upload_id: <id>` dans
    le body.
    """
    # Rate limit user-scoped — 20 uploads/heure.
    await check_user_rate_limit(
        current_user.id,
        action="file_upload",
        max_requests=settings.files_upload_rate_limit_per_hour,
        window_seconds=3600,
        on_exceeded=RateLimitAbuseException,
    )

    row = await FileUploadService.upload(
        current_user,
        db,
        upload_file=file,
        extract_text_enabled=extract_text,
        scan_virus=scan_virus,
    )
    return NexyaResponse(success=True, data=await _upload_to_response(row))


@router.get(
    "/{upload_id}",
    response_model=NexyaResponse[UploadedFileResponse],
)
async def get_uploaded_file(
    upload_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[UploadedFileResponse]:
    """Lit la métadonnée d'un upload existant — incluant la sentinelle RAG
    `chunks_indexed_at` que le client utilise pour poller l'état d'indexation.

    Owner check 404 IDOR-safe (un user ne peut pas lire l'upload d'un autre,
    même en cas de fuite d'UUID).

    La presigned URL retournée est **régénérée à la volée** (TTL 30 min) à
    chaque appel — le client peut donc re-fetcher cet endpoint pour
    rafraîchir une URL expirée sans avoir à re-uploader.

    Cas d'usage principal côté Flutter (D2) : polling exponentiel
    5/10/20/30 s (cap 5 min) sur les MIMEs chunking-éligibles
    (PDF/DOCX/TXT/MD) jusqu'à `chunks_indexed_at != null`. Le client doit
    annuler le polling à `dispose()` du provider et arrêter le timer dès
    le flip pour ne pas saturer le backend.

    Codes d'erreur :
    - **404** `RESOURCE_NOT_FOUND` si l'upload n'existe pas, est soft-deleted,
      ou n'appartient pas à l'utilisateur courant.
    """
    row = await FileUploadService.get_for_user(upload_id, current_user, db)
    return NexyaResponse(success=True, data=await _upload_to_response(row))
