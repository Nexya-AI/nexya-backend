"""
Router Projects — 9 endpoints sous le préfixe `/projects`.

Session C2 — alignement strict avec la convention NEXYA (cf. chat/router Lot 3) :

1.  `POST    /projects`                                 — create (201)
2.  `GET     /projects`                                 — list paginé + q
3.  `GET     /projects/{id}`                            — détail
4.  `PATCH   /projects/{id}`                            — update partiel
5.  `DELETE  /projects/{id}`                            — soft-delete (204)
6.  `GET     /projects/{id}/conversations`              — convs rattachées
7.  `POST    /projects/{id}/files`                      — create file metadata
8.  `GET     /projects/{id}/files`                      — liste fichiers
9.  `DELETE  /projects/{id}/files/{file_id}`            — soft-delete file (204)

Aligné sur les invariants NEXYA :
- `NexyaResponse[T]` partout sauf 204 DELETE.
- `PATCH` et non `PUT` (alignement C1 Lot 3 `PATCH /chat/conversations/{id}`).
- 404 IDOR-safe (service lève `ResourceNotFoundException`).
- Pagination keyset cursor ≤ 50.
- `get_current_user` sur TOUTES les routes (auth obligatoire).
- Aucune logique métier ici — chaque endpoint délègue à `ProjectService` ou
  à `ConversationService.list_for_user(project_id=...)` (C1 FTS réutilisé).

L'endpoint `POST /projects/{id}/files` crée une MÉTADONNÉE. L'upload
physique (MinIO/S3, extraction MIME, scan ClamAV) sera livré en session E3
(`POST /files/upload`). `storage_key` reste nullable tant que E3 n'est pas
câblée — le Flutter peut déjà persister nom + type, l'upload physique
viendra enrichir l'entrée.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat.schemas import ConversationListItem, ConversationsPage
from app.features.chat.service import ConversationService
from app.features.files.models import UploadedFile
from app.features.projects.models import ProjectFile
from app.features.projects.schemas import (
    ProjectCreate,
    ProjectFileCreate,
    ProjectFileResponse,
    ProjectFilesPage,
    ProjectListItem,
    ProjectResponse,
    ProjectsPage,
    ProjectUpdate,
)
from app.features.projects.service import ProjectService
from app.shared.schemas import NexyaResponse

log = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["projects"])


# ══════════════════════════════════════════════════════════════
# CRUD PROJECTS
# ══════════════════════════════════════════════════════════════


@router.post(
    "",
    response_model=NexyaResponse[ProjectResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    body: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ProjectResponse]:
    """Crée un projet pour l'utilisateur courant.

    - **402** `PROJECT_QUOTA_EXCEEDED` si le plan Free/Pro est déjà plein
      (`data.current`, `data.max`, `data.plan`).
    - **409** `PROJECT_NAME_CONFLICT` si un projet actif du même nom
      (case-insensitive) existe déjà pour cet utilisateur.
    - **422** `VALIDATION_ERROR` si `name` vide, `icon_index` hors [0..24],
      `color_index` hors [0..17], `instructions` > 4000 chars.
    """
    view = await ProjectService.create(body, current_user, db)
    payload = _project_view_to_response(view)
    return NexyaResponse(success=True, data=payload)


@router.get(
    "",
    response_model=NexyaResponse[ProjectsPage],
)
async def list_projects(
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Curseur opaque renvoyé par la page précédente.",
    ),
    limit: int = Query(default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."),
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description=("Recherche fuzzy (trigram) sur `name`. Absent ou vide = pas de filtre."),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ProjectsPage]:
    """Liste paginée des projets actifs — tri récence DESC (`created_at`).

    `next_cursor=null` = fin de liste. Chaque item inclut `file_count`,
    `conversation_count` et `last_activity_at` pour que l'UI Flutter
    puisse trier/afficher la jauge sans appel supplémentaire.
    """
    page = await ProjectService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        q=q,
    )
    return NexyaResponse(
        success=True,
        data=ProjectsPage(
            items=[_project_listitem_orm_to_schema(item) for item in page.items],
            next_cursor=page.next_cursor,
        ),
    )


@router.get(
    "/{project_id}",
    response_model=NexyaResponse[ProjectResponse],
)
async def get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ProjectResponse]:
    """Détail d'un projet — 404 IDOR-safe si pas propriétaire."""
    view = await ProjectService.get(project_id, current_user, db)
    return NexyaResponse(success=True, data=_project_view_to_response(view))


@router.patch(
    "/{project_id}",
    response_model=NexyaResponse[ProjectResponse],
)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ProjectResponse]:
    """Mise à jour partielle — sémantique PATCH stricte.

    Un champ absent n'est pas touché. Pour effacer `instructions`, envoyer
    `clear_instructions=true` (pas `instructions=null`, ambigu).
    """
    view = await ProjectService.update(project_id, body, current_user, db)
    return NexyaResponse(success=True, data=_project_view_to_response(view))


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete le projet et détache ses conversations actives.

    Les conversations continuent d'exister sans rattachement projet — elles
    apparaissent dans la liste générale `/chat/conversations` sans filtre.
    """
    await ProjectService.soft_delete(project_id, current_user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════
# RELATION — conversations rattachées (réutilise C1 FTS)
# ══════════════════════════════════════════════════════════════


@router.get(
    "/{project_id}/conversations",
    response_model=NexyaResponse[ConversationsPage],
)
async def list_project_conversations(
    project_id: uuid.UUID,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=20, ge=1, le=50),
    q: str | None = Query(default=None, min_length=1, max_length=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationsPage]:
    """Liste paginée des conversations actives rattachées au projet.

    Pipeline :
    1. **Owner check** du projet (`_get_owned_project` → 404 IDOR si pas
       propriétaire).
    2. **Délégation** à `ConversationService.list_for_user` avec le kwarg
       `project_id=project_id` — réutilise intégralement le pipeline FTS
       français de C1 (`q` optionnel, keyset `(last_message_at, id) DESC`,
       index partiel `idx_conversations_project`).
    """
    await ProjectService._get_owned_project(project_id, current_user.id, db)
    page = await ConversationService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        q=q,
        project_id=project_id,
    )
    return NexyaResponse(
        success=True,
        data=ConversationsPage(
            items=[ConversationListItem.model_validate(c) for c in page.items],
            next_cursor=page.next_cursor,
        ),
    )


# ══════════════════════════════════════════════════════════════
# FILES — métadonnée seule (upload physique = E3)
# ══════════════════════════════════════════════════════════════


@router.post(
    "/{project_id}/files",
    response_model=NexyaResponse[ProjectFileResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_project_file(
    project_id: uuid.UUID,
    body: ProjectFileCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ProjectFileResponse]:
    """Crée une métadonnée de fichier rattachée au projet.

    C2 livre la métadonnée uniquement. L'upload physique (MinIO/S3,
    extraction MIME, scan ClamAV) arrive en session E3.

    - **402** `PROJECT_FILES_QUOTA_EXCEEDED` si le projet a déjà atteint
      son plafond pour le plan courant (`Free=5`, `Pro=100`).
    - **422** `VALIDATION_ERROR` si `file_type` hors enum, `name` vide.
    """
    pfile = await ProjectService.add_file(project_id, body, current_user, db)
    return NexyaResponse(
        success=True,
        data=await _project_file_to_response(pfile, db),
    )


@router.get(
    "/{project_id}/files",
    response_model=NexyaResponse[ProjectFilesPage],
)
async def list_project_files(
    project_id: uuid.UUID,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ProjectFilesPage]:
    """Liste paginée des fichiers actifs — tri `uploaded_at DESC`."""
    page = await ProjectService.list_files(
        project_id,
        current_user,
        db,
        cursor=cursor,
        limit=limit,
    )
    items = await _project_files_to_responses(page.items, db)
    return NexyaResponse(
        success=True,
        data=ProjectFilesPage(
            items=items,
            next_cursor=page.next_cursor,
        ),
    )


@router.delete(
    "/{project_id}/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_project_file(
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete un fichier du projet — 204 idempotent."""
    await ProjectService.remove_file(project_id, file_id, current_user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════
# HELPERS — traduction ORM/scalar → schémas Pydantic
# ══════════════════════════════════════════════════════════════


async def _project_file_to_response(
    pfile: ProjectFile,
    db: AsyncSession,
) -> ProjectFileResponse:
    """Enrichit un `ProjectFile` ORM avec presigned URL + sentinelle RAG +
    upload_id résolus à la volée (D2.5).

    - `presigned_url` : signé via `ObjectStore.generate_presigned_url` si
      `storage_key` non-null. TTL contrôlé par `settings.files_presigned_ttl_seconds`.
    - `upload_id` + `chunks_indexed_at` : recherchés via
      `UploadedFile WHERE attached_to_kind='project_file' AND attached_to_id=pfile.id`.
      Idempotent : si un upload a été ré-attaché à un autre target,
      `mark_attached` a écrasé l'ancien lien (cf. `FileUploadService.mark_attached`).
      `None` si mode legacy (pas d'upload_id passé en C2 pur).

    Pour la liste paginée, préférer `_project_files_to_responses` qui
    bulk-query les UploadedFile en 1 SQL au lieu de N+1.
    """
    presigned_url: str | None = None
    if pfile.storage_key is not None:
        from app.core.storage import get_object_store

        store = get_object_store()
        ttl = settings.files_presigned_ttl_seconds
        presigned_url = await store.generate_presigned_url(
            pfile.storage_key, ttl_seconds=ttl, method="GET"
        )

    upload_id: uuid.UUID | None = None
    chunks_indexed_at = None
    upload_row = (
        await db.execute(
            select(UploadedFile).where(
                UploadedFile.attached_to_kind == "project_file",
                UploadedFile.attached_to_id == pfile.id,
                UploadedFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if upload_row is not None:
        upload_id = upload_row.id
        chunks_indexed_at = upload_row.chunks_indexed_at

    return ProjectFileResponse(
        id=pfile.id,
        project_id=pfile.project_id,
        name=pfile.name,
        file_type=pfile.file_type,  # type: ignore[arg-type]
        storage_key=pfile.storage_key,
        size_bytes=pfile.size_bytes,
        mime_type=pfile.mime_type,
        uploaded_at=pfile.uploaded_at,
        created_at=pfile.created_at,
        updated_at=pfile.updated_at,
        presigned_url=presigned_url,
        upload_id=upload_id,
        chunks_indexed_at=chunks_indexed_at,
    )


async def _project_files_to_responses(
    pfiles: list[ProjectFile],
    db: AsyncSession,
) -> list[ProjectFileResponse]:
    """Variante bulk anti-N+1 de `_project_file_to_response` pour le listing.

    Pour N fichiers, fait :
    - 1 seule SELECT bulk sur `uploaded_files` avec `attached_to_id IN (...)`.
    - N appels `generate_presigned_url` (impossible à bulker côté MinIO,
      mais c'est local au pod et chacun est ~1 ms).
    """
    if not pfiles:
        return []

    # Bulk fetch des uploads attachés à ces project_files.
    pfile_ids = [pf.id for pf in pfiles]
    uploads_by_target: dict[uuid.UUID, UploadedFile] = {}
    if pfile_ids:
        result = await db.execute(
            select(UploadedFile).where(
                UploadedFile.attached_to_kind == "project_file",
                UploadedFile.attached_to_id.in_(pfile_ids),
                UploadedFile.deleted_at.is_(None),
            )
        )
        for u in result.scalars().all():
            if u.attached_to_id is not None:
                uploads_by_target[u.attached_to_id] = u

    from app.core.storage import get_object_store

    store = get_object_store()
    ttl = settings.files_presigned_ttl_seconds

    responses: list[ProjectFileResponse] = []
    for pf in pfiles:
        presigned_url: str | None = None
        if pf.storage_key is not None:
            presigned_url = await store.generate_presigned_url(
                pf.storage_key, ttl_seconds=ttl, method="GET"
            )

        upload_row = uploads_by_target.get(pf.id)
        responses.append(
            ProjectFileResponse(
                id=pf.id,
                project_id=pf.project_id,
                name=pf.name,
                file_type=pf.file_type,  # type: ignore[arg-type]
                storage_key=pf.storage_key,
                size_bytes=pf.size_bytes,
                mime_type=pf.mime_type,
                uploaded_at=pf.uploaded_at,
                created_at=pf.created_at,
                updated_at=pf.updated_at,
                presigned_url=presigned_url,
                upload_id=upload_row.id if upload_row else None,
                chunks_indexed_at=upload_row.chunks_indexed_at if upload_row else None,
            )
        )
    return responses


def _project_view_to_response(view) -> ProjectResponse:
    """Combine un `Project` ORM + `file_count` + `conversation_count` en
    `ProjectResponse`. Pydantic ne fait pas ça nativement depuis un dataclass
    non-BaseModel — on compose explicitement."""
    project = view.project
    return ProjectResponse(
        id=project.id,
        user_id=project.user_id,
        name=project.name,
        icon_index=project.icon_index,
        color_index=project.color_index,
        instructions=project.instructions,
        file_count=view.file_count,
        conversation_count=view.conversation_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
        deleted_at=project.deleted_at,
    )


def _project_listitem_orm_to_schema(item) -> ProjectListItem:
    """Convertit un `ProjectListItemOrm` en `ProjectListItem` Pydantic."""
    p = item.project
    return ProjectListItem(
        id=p.id,
        name=p.name,
        icon_index=p.icon_index,
        color_index=p.color_index,
        file_count=item.file_count,
        conversation_count=item.conversation_count,
        last_activity_at=item.last_activity_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )
