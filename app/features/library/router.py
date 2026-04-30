"""
Router Library — 4 endpoints sous `/library` (Session C3).

Convention NEXYA :
- `NexyaResponse[T]` pour POST / GET, 204 (sans body) pour DELETE.
- `get_current_user` sur toutes les routes.
- 404 IDOR-safe (service lève `ResourceNotFoundException`).
- Pagination cursor-based ≤ 50.

Aucune logique métier ici — le service fait tout. Le router se contente
de la traduction ORM → Pydantic et de l'enrichissement `presigned_url`
(appel synchrone au `ObjectStore.generate_presigned_url`, HMAC local,
coût négligeable).
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.library.models import LibraryItem
from app.features.library.schemas import (
    LibraryItemCreate,
    LibraryItemListItem,
    LibraryItemResponse,
    LibraryItemType,
    LibraryPage,
    LibrarySource,
)
from app.features.library.service import LibraryService
from app.shared.schemas import NexyaResponse

log = structlog.get_logger()

router = APIRouter(prefix="/library", tags=["library"])


# ══════════════════════════════════════════════════════════════
# Helpers — enrichissement presigned URL
# ══════════════════════════════════════════════════════════════


async def _item_to_response(item: LibraryItem) -> LibraryItemResponse:
    """Combine une `LibraryItem` ORM avec sa presigned URL fraîche."""
    url = await LibraryService.presigned_url_for(item)
    return LibraryItemResponse(
        id=item.id,
        user_id=item.user_id,
        type=item.type,  # type: ignore[arg-type]
        file_type=item.file_type,  # type: ignore[arg-type]
        title=item.title,
        description=item.description,
        url=url,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        width_px=item.width_px,
        height_px=item.height_px,
        duration_ms=item.duration_ms,
        aspect_ratio=item.aspect_ratio,
        source=item.source,  # type: ignore[arg-type]
        provider=item.provider,
        model=item.model,
        prompt=item.prompt,
        source_conversation_id=item.source_conversation_id,
        source_message_id=item.source_message_id,
        tags=item.tags,
        metadata_json=item.metadata_json,
        created_at=item.created_at,
        updated_at=item.updated_at,
        deleted_at=item.deleted_at,
    )


async def _item_to_list_item(item: LibraryItem) -> LibraryItemListItem:
    """Version allégée pour les grilles — conserve url + type + taille."""
    url = await LibraryService.presigned_url_for(item)
    return LibraryItemListItem(
        id=item.id,
        type=item.type,  # type: ignore[arg-type]
        file_type=item.file_type,  # type: ignore[arg-type]
        title=item.title,
        url=url,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        width_px=item.width_px,
        height_px=item.height_px,
        duration_ms=item.duration_ms,
        aspect_ratio=item.aspect_ratio,
        source=item.source,  # type: ignore[arg-type]
        source_conversation_id=item.source_conversation_id,
        tags=item.tags,
        created_at=item.created_at,
    )


# ══════════════════════════════════════════════════════════════
# 1. POST /library — create avec base64
# ══════════════════════════════════════════════════════════════


@router.post(
    "",
    response_model=NexyaResponse[LibraryItemResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_library_item(
    body: LibraryItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[LibraryItemResponse]:
    """Sauve un média dans la biblio utilisateur avec le binaire en base64.

    - **402** `LIBRARY_QUOTA_EXCEEDED` si plafond plan atteint.
    - **413** `FILE_TOO_LARGE` si binaire > 20 MB décodé.
    - **422** `VALIDATION_ERROR` si base64 invalide, type/mime/file_type
      incohérents, tags malformés.
    - **503** `STORAGE_UNAVAILABLE` si MinIO/S3 down.

    **Dédup** : si le même contenu (SHA-256 identique) a déjà été sauvé
    par le même user, on renvoie l'entrée existante — **pas d'erreur**,
    pas de double-upload storage. UX idempotente.
    """
    item = await LibraryService.create_from_base64(current_user, db, body)
    return NexyaResponse(success=True, data=await _item_to_response(item))


# ══════════════════════════════════════════════════════════════
# 2. GET /library — liste paginée avec filtres combinables
# ══════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=NexyaResponse[LibraryPage],
)
async def list_library_items(
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Curseur opaque renvoyé par la page précédente.",
    ),
    limit: int = Query(default=20, ge=1, le=50),
    type: LibraryItemType | None = Query(
        default=None,
        description="Filtre par type : image, video, gif, audio, document, text.",
    ),
    source: LibrarySource | None = Query(
        default=None,
        description="Filtre par source : generated, uploaded, imported, shared.",
    ),
    conversation_id: uuid.UUID | None = Query(
        default=None,
        description="Filtre : médias issus de cette conversation.",
    ),
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description="Recherche fuzzy (trigram) sur le titre.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[LibraryPage]:
    """Liste paginée des médias actifs — tri `created_at DESC`.

    Chaque item inclut une presigned URL MinIO valide 1 h.
    `next_cursor=null` = fin de liste.
    """
    page = await LibraryService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        type_=type,
        source=source,
        conversation_id=conversation_id,
        q=q,
    )
    items = [await _item_to_list_item(i) for i in page.items]
    return NexyaResponse(
        success=True,
        data=LibraryPage(items=items, next_cursor=page.next_cursor),
    )


# ══════════════════════════════════════════════════════════════
# 3. GET /library/{id} — détail + presigned URL
# ══════════════════════════════════════════════════════════════


@router.get(
    "/{item_id}",
    response_model=NexyaResponse[LibraryItemResponse],
)
async def get_library_item(
    item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[LibraryItemResponse]:
    """Détail d'un média — 404 IDOR-safe si pas propriétaire."""
    item = await LibraryService.get(item_id, current_user, db)
    return NexyaResponse(success=True, data=await _item_to_response(item))


# ══════════════════════════════════════════════════════════════
# 4. DELETE /library/{id} — soft-delete
# ══════════════════════════════════════════════════════════════


@router.delete(
    "/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_library_item(
    item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete le média — 204 idempotent (via 404 sur 2ᵉ appel).

    Pas de suppression MinIO synchrone — un cron de Phase 12 purgera les
    binaires des items `deleted_at < NOW() - 7 days`. Cette marge permet
    un éventuel restore futur sans perte.
    """
    await LibraryService.soft_delete(item_id, current_user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
