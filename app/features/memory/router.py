"""
Router Mémoire IA — endpoints publics D5.

4 endpoints sous le prefix `/memory` :
- `POST /memory/index` : ajout manuel d'un fait durable (source=`manual`).
- `POST /memory/search` : recherche sémantique top-K dans les mémoires.
- `GET /memory`        : liste paginée keyset, filtrable par `source`.
- `DELETE /memory/{id}` : hard-delete RGPD Article 17 idempotent.

Discipline :
- Aucune logique métier dans le router — délégation stricte à
  `MemoryStore`. Le router ne fait que binder Pydantic ↔ FastAPI,
  appeler le service, et emballer en `NexyaResponse[T]`.
- `DELETE` renvoie 204 **toujours** (même si la mémoire n'existait pas)
  pour ne pas confirmer l'existence à un attaquant énumérateur.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.memory.schemas import (
    MemoryCreate,
    MemoryListResponse,
    MemoryResponse,
    MemorySearchItem,
    MemorySearchRequest,
    MemorySearchResponse,
)
from app.features.memory.service import MemoryStore
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/memory", tags=["memory"])


# ══════════════════════════════════════════════════════════════
# POST /memory/index
# ══════════════════════════════════════════════════════════════


@router.post(
    "/index",
    status_code=status.HTTP_201_CREATED,
    response_model=NexyaResponse[MemoryResponse],
)
async def create_memory(
    body: MemoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[MemoryResponse]:
    """Ajout manuel d'un fait durable (source='manual').

    Consomme 1 crédit embeddings (`BudgetTracker`) + 1 slot quota plan.
    Dédup SHA-256 : re-add du même contenu retourne l'entrée existante.
    """
    memory = await MemoryStore.add(
        current_user,
        db,
        content=body.content,
        source="manual",
        importance=body.importance,
        metadata_json=body.metadata_json,
    )
    return NexyaResponse(success=True, data=MemoryResponse.model_validate(memory))


# ══════════════════════════════════════════════════════════════
# POST /memory/search
# ══════════════════════════════════════════════════════════════


@router.post(
    "/search",
    response_model=NexyaResponse[MemorySearchResponse],
)
async def search_memories(
    body: MemorySearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[MemorySearchResponse]:
    """Recherche sémantique top-K dans les mémoires de l'user.

    Consomme 1 crédit embeddings (embed de la query). La recherche
    HNSW elle-même est gratuite (locale Postgres).
    """
    results = await MemoryStore.search(
        current_user,
        db,
        query=body.query,
        k=body.k,
        min_similarity=body.min_similarity,
        source=body.source,
    )
    items = [
        MemorySearchItem(
            memory=MemoryResponse.model_validate(r.memory),
            similarity=r.similarity,
        )
        for r in results
    ]
    return NexyaResponse(
        success=True,
        data=MemorySearchResponse(items=items),
    )


# ══════════════════════════════════════════════════════════════
# GET /memory
# ══════════════════════════════════════════════════════════════


@router.get(
    "",
    response_model=NexyaResponse[MemoryListResponse],
)
async def list_memories(
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=20, ge=1, le=50),
    source: str | None = Query(default=None, max_length=16),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[MemoryListResponse]:
    """Liste paginée keyset DESC des mémoires actives de l'user.

    `source` ∈ {`manual`, `extracted`, `imported`, `system`} filtre
    optionnel — permet à l'UI « Ma mémoire » de séparer « ce que J'AI
    ajouté » de « ce que l'IA a extrait ».
    """
    page = await MemoryStore.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        source=source,
    )
    return NexyaResponse(
        success=True,
        data=MemoryListResponse(
            items=[MemoryResponse.model_validate(m) for m in page.items],
            next_cursor=page.next_cursor,
        ),
    )


# ══════════════════════════════════════════════════════════════
# DELETE /memory/{id}
# ══════════════════════════════════════════════════════════════


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hard-delete RGPD Article 17.

    Idempotent : 204 même si la mémoire n'existait pas (ou n'était pas
    à l'user). On ne révèle pas l'existence à un attaquant.
    """
    await MemoryStore.delete_one_for_user(current_user, db, memory_id=memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
