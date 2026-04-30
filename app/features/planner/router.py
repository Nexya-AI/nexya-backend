"""
Router Planner `/tasks/*` — 8 endpoints (F1).

Tous `Depends(get_current_user)`. Aucune logique métier — délégation
stricte à `TaskSchedulerService`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.planner.schemas import (
    TaskCreate,
    TaskResponse,
    TaskResultResponse,
    TaskResultsPage,
    TasksPage,
    TaskUpdate,
)
from app.features.planner.service import TaskSchedulerService
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ══════════════════════════════════════════════════════════════
# POST /tasks
# ══════════════════════════════════════════════════════════════


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=NexyaResponse[TaskResponse],
)
async def create_task(
    body: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TaskResponse]:
    task = await TaskSchedulerService.create_task(current_user, body, db)
    return NexyaResponse(success=True, data=TaskResponse.model_validate(task))


# ══════════════════════════════════════════════════════════════
# GET /tasks
# ══════════════════════════════════════════════════════════════


@router.get("", response_model=NexyaResponse[TasksPage])
async def list_tasks(
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=20, ge=1, le=50),
    status_filter: str | None = Query(default=None, alias="status", max_length=16),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TasksPage]:
    page = await TaskSchedulerService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        status=status_filter,
    )
    return NexyaResponse(
        success=True,
        data=TasksPage(
            items=[TaskResponse.model_validate(t) for t in page.items],
            next_cursor=page.next_cursor,
        ),
    )


# ══════════════════════════════════════════════════════════════
# GET /tasks/{id}
# ══════════════════════════════════════════════════════════════


@router.get("/{task_id}", response_model=NexyaResponse[TaskResponse])
async def get_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TaskResponse]:
    task = await TaskSchedulerService.get_task(task_id, current_user, db)
    return NexyaResponse(success=True, data=TaskResponse.model_validate(task))


# ══════════════════════════════════════════════════════════════
# PATCH /tasks/{id}
# ══════════════════════════════════════════════════════════════


@router.patch("/{task_id}", response_model=NexyaResponse[TaskResponse])
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TaskResponse]:
    task = await TaskSchedulerService.update_task(task_id, current_user, body, db)
    return NexyaResponse(success=True, data=TaskResponse.model_validate(task))


# ══════════════════════════════════════════════════════════════
# DELETE /tasks/{id}
# ══════════════════════════════════════════════════════════════


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await TaskSchedulerService.soft_delete_task(task_id, current_user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════
# POST /tasks/{id}/pause
# ══════════════════════════════════════════════════════════════


@router.post("/{task_id}/pause", response_model=NexyaResponse[TaskResponse])
async def pause_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TaskResponse]:
    task = await TaskSchedulerService.pause_task(task_id, current_user, db)
    return NexyaResponse(success=True, data=TaskResponse.model_validate(task))


# ══════════════════════════════════════════════════════════════
# POST /tasks/{id}/resume
# ══════════════════════════════════════════════════════════════


@router.post("/{task_id}/resume", response_model=NexyaResponse[TaskResponse])
async def resume_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TaskResponse]:
    task = await TaskSchedulerService.resume_task(task_id, current_user, db)
    return NexyaResponse(success=True, data=TaskResponse.model_validate(task))


# ══════════════════════════════════════════════════════════════
# GET /tasks/{id}/results
# ══════════════════════════════════════════════════════════════


@router.get(
    "/{task_id}/results",
    response_model=NexyaResponse[TaskResultsPage],
)
async def list_task_results(
    task_id: uuid.UUID,
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TaskResultsPage]:
    page = await TaskSchedulerService.list_results(
        task_id, current_user, db, cursor=cursor, limit=limit
    )
    return NexyaResponse(
        success=True,
        data=TaskResultsPage(
            items=[TaskResultResponse.model_validate(r) for r in page.items],
            next_cursor=page.next_cursor,
        ),
    )
