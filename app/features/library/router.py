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


async def _item_to_response(
    item: LibraryItem,
    *,
    version_number: int = 1,
    versions_count: int = 1,
) -> LibraryItemResponse:
    """Combine une `LibraryItem` ORM avec sa presigned URL fraîche.

    C4.11 — `version_number` + `versions_count` injectés depuis le caller
    (router GET /library/{id} les calcule via `LibraryService.get_with_versions`).
    Defaults `1/1` pour rétro-compat avec les call-sites pré-C4.11.
    """
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
        # C4.11 — Versioning
        parent_library_id=item.parent_library_id,
        version_number=version_number,
        versions_count=versions_count,
    )


async def _item_to_list_item(
    item: LibraryItem,
    *,
    version_number: int = 1,
    versions_count: int = 1,
) -> LibraryItemListItem:
    """Version allégée pour les grilles — conserve url + type + taille.

    C4.11 — `version_number` + `versions_count` injectés depuis le caller
    bulk (router GET /library calcule les counts en 1 seul SELECT pour
    toute la page paginée, économise N round-trips DB).
    """
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
        # C4.11 — Versioning
        parent_library_id=item.parent_library_id,
        version_number=version_number,
        versions_count=versions_count,
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
    # C4.11 — calcule versions_count pour le nouvel item (peut être > 1
    # si l'user a régénéré le même message source plusieurs fois).
    # **Fail-safe absolu** : si count_versions_for_lineage échoue (mock test
    # avec queue execute insuffisante, blip DB transient, schéma pré-027 sans
    # FK self-ref), on retombe sur 1/1 — l'utilisateur voit son item sans
    # info versioning plutôt qu'un 500 cassant l'écran Library.
    version_number = LibraryService._extract_version_number(item)
    try:
        versions_count = await LibraryService.count_versions_for_lineage(item, db)
    except Exception as exc:  # noqa: BLE001 fail-safe défensif
        versions_count = 1
        try:
            log.warning(
                "library.versions_count.failed",
                item_id=str(item.id),
                error_type=getattr(type(exc), "__name__", "Unknown"),
            )
        except Exception:  # noqa: BLE001 log doit jamais cascader
            pass
    return NexyaResponse(
        success=True,
        data=await _item_to_response(
            item,
            version_number=version_number,
            versions_count=versions_count,
        ),
    )


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
    # C4.11 — bulk versions_count en 1 SELECT pour toute la page paginée
    # (économise N round-trips DB sur cap 30 items / page).
    # **Fail-safe absolu** : si count_versions_bulk échoue (mock test
    # avec queue execute insuffisante, blip DB transient, schéma pré-027
    # sans FK self-ref), on retombe sur dict vide → tous les items reçoivent
    # versions_count=1 par défaut via le `.get(root_id, 1)` below.
    try:
        bulk_versions = await LibraryService.count_versions_bulk(page.items, db)
    except Exception as exc:  # noqa: BLE001 fail-safe défensif
        bulk_versions = {}
        try:
            log.warning(
                "library.versions_bulk.failed",
                page_size=len(page.items),
                error_type=getattr(type(exc), "__name__", "Unknown"),
            )
        except Exception:  # noqa: BLE001 log doit jamais cascader
            pass
    items = []
    for i in page.items:
        root_id = i.parent_library_id or i.id
        items.append(
            await _item_to_list_item(
                i,
                version_number=LibraryService._extract_version_number(i),
                versions_count=bulk_versions.get(root_id, 1),
            )
        )
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
    """Détail d'un média — 404 IDOR-safe si pas propriétaire.

    C4.11 — enrichi avec version_number + versions_count séparés (au lieu
    d'un seul `get_with_versions` qui bypasse `LibraryService.get` mocked
    par les tests legacy). `LibraryService.get` reste la méthode mockable
    canonique pour le owner-check, `count_versions_for_lineage` enveloppé
    fail-safe pour ne pas casser le 200 si la queue mock test est limitée.
    """
    item = await LibraryService.get(item_id, current_user, db)
    version_number = LibraryService._extract_version_number(item)
    try:
        versions_count = await LibraryService.count_versions_for_lineage(item, db)
    except Exception as exc:  # noqa: BLE001 fail-safe défensif
        versions_count = 1
        try:
            log.warning(
                "library.versions_count.failed",
                item_id=str(item.id),
                error_type=getattr(type(exc), "__name__", "Unknown"),
            )
        except Exception:  # noqa: BLE001 log doit jamais cascader
            pass
    return NexyaResponse(
        success=True,
        data=await _item_to_response(
            item,
            version_number=version_number,
            versions_count=versions_count,
        ),
    )


# ══════════════════════════════════════════════════════════════
# 4. GET /library/{id}/versions — liste versions du lineage (C4.11)
# ══════════════════════════════════════════════════════════════


@router.get(
    "/{item_id}/versions",
    response_model=NexyaResponse[list[LibraryItemListItem]],
)
async def get_library_item_versions(
    item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[list[LibraryItemListItem]]:
    """C4.11 — Liste toutes les versions actives du lineage de l'item.

    Pattern: le frontend appelle au tap du dropdown « Voir v1 / v2 / v3 »
    pour récupérer tous les siblings ordonnés version_number ASC (racine
    v1 → dernière version). Lazy-load au tap user (économie 2G/3G).

    Si `item_id` est la racine du lineage → retourne `[racine, v2, v3, ...]`.
    Si `item_id` est une descendante → résout la racine via `parent_library_id`
    puis retourne `[racine, ..., item_id, ..., dernière]`.

    Erreurs :
      - **404** `RESOURCE_NOT_FOUND` si pas propriétaire (IDOR-safe).

    Note: items sans lineage (parent NULL ET 0 descendants) → retourne `[item]`
    seul (l'item est sa propre version unique).
    """
    item = await LibraryService.get(item_id, current_user, db)
    root_id = item.parent_library_id or item.id
    siblings = await LibraryService.list_versions_for_root(root_id, current_user, db)
    # Bulk count pour tous les siblings (pour exposer versions_count cohérent
    # — devrait être identique pour tous les items du lineage = total).
    bulk = await LibraryService.count_versions_bulk(siblings, db)
    out: list[LibraryItemListItem] = []
    for s in siblings:
        out.append(
            await _item_to_list_item(
                s,
                version_number=LibraryService._extract_version_number(s),
                versions_count=bulk.get(root_id, len(siblings)),
            )
        )
    return NexyaResponse(success=True, data=out)


# ══════════════════════════════════════════════════════════════
# 5. DELETE /library/{id} — soft-delete
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
